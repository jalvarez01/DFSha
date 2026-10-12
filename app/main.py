"""
Punto de entrada de la aplicación FastAPI.

Este archivo SOLO se encarga de:
- Crear las tablas (create_all), migrar las de versiones anteriores y
  sembrar el directorio raíz al arrancar.
- Decidir el rol de HA (líder o respaldo) y arrancar los bucles de fondo:
  seguimiento/failover entre ControlNodes y re-replicación de bloques.
- Exponer /health.
- Registrar los routers (ver abajo; el orden de /files importa).

Los endpoints de negocio (ls/mkdir/rmdir/rm, put/get) NO se implementan
aquí: cada compañero debe crear su propio `APIRouter` en un módulo nuevo
(p. ej. app/routers/fs.py, app/routers/transfer.py) e incluirlo abajo con
`app.include_router(...)`, usando los modelos de app/schemas.py y las
funciones de app/path_service.py.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, inspect, select, text
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.ha import changelog
from app.ha import manager as ha_manager
from app.ha.middleware import HAMiddleware
from app.ha.state import state as ha_state
from app.models import BLOCKS_UNIQUE_CONSTRAINT, Block, BlockReplica, User
from app.path_service import ROOT_MODE, get_or_create_root
from app.replication import PENDING, STORED, check_replication
from app.schemas import ErrorResponse

logger = logging.getLogger("dfsha")
# Los mensajes de HA y re-replicación (loggers "dfsha.*") se ven en la
# salida del servidor junto a los de uvicorn.
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def _migrate_blocks_table(bind: Engine) -> None:
    """Migra la tabla `blocks` de BD creadas antes del reemplazo de bloques.

    La unicidad pasó de (file_id, block_index) a (file_id, block_index,
    committed) para que un reemplazo pendiente conviva con el bloque
    confirmado que sigue sirviéndose (ver app/models.py). `create_all` no
    altera tablas existentes y SQLite no permite cambiar una restricción en
    sitio, así que se reconstruye la tabla conservando las filas.
    """
    inspector = inspect(bind)
    if "blocks" not in inspector.get_table_names():
        return
    names = {uc["name"] for uc in inspector.get_unique_constraints("blocks")}
    if BLOCKS_UNIQUE_CONSTRAINT in names:
        return

    columns = ", ".join(column.name for column in Block.__table__.columns)
    # Los índices conservan su nombre tras el RENAME: hay que soltarlos para
    # que create() pueda volver a crearlos sobre la tabla nueva.
    index_names = [index["name"] for index in inspector.get_indexes("blocks")]
    with bind.begin() as conn:
        conn.execute(text("ALTER TABLE blocks RENAME TO blocks_old"))
        for name in index_names:
            conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
        Block.__table__.create(conn)
        conn.execute(text(f"INSERT INTO blocks ({columns}) SELECT {columns} FROM blocks_old"))
        conn.execute(text("DROP TABLE blocks_old"))


def _add_missing_columns(bind: Engine) -> set[tuple[str, str]]:
    """Añade a las tablas existentes las columnas nuevas de los modelos.

    `create_all` crea tablas nuevas pero no altera las que ya existen, así
    que una BD de un hito anterior quedaría sin, p. ej., `files.mode`.
    SQLite sí permite ADD COLUMN; las columnas NOT NULL nuevas traen un
    `server_default` en el modelo para poder añadirse con filas existentes.
    Devuelve las (tabla, columna) añadidas.
    """
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    added: set[tuple[str, str]] = set()
    with bind.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {column.type.compile(bind.dialect)}'
                if column.server_default is not None:
                    ddl += f" DEFAULT '{column.server_default.arg}'"
                if not column.nullable and column.server_default is not None:
                    ddl += " NOT NULL"
                conn.execute(text(ddl))
                added.add((table.name, column.name))
    return added


def prepare_database() -> set[tuple[str, str]]:
    """Esquema al día (todos los nodos, líder o respaldo)."""
    Base.metadata.create_all(bind=engine)
    _migrate_blocks_table(engine)
    return _add_missing_columns(engine)


def seed_and_migrate_data(added_columns: set[tuple[str, str]] = frozenset()) -> None:
    """Datos mínimos y migración de datos. Solo en el líder (en un respaldo
    llegan por el log o el snapshot):

    - Usuario 'system' (dueño de la raíz, no puede iniciar sesión) y raíz "/".
    - Si la BD viene de antes del control de acceso: la raíz pasa a 1777.
    - Bloques de antes de la replicación (sin filas en `block_replicas`):
      su única copia es la de `blocks.datanode_id`.
    """
    db = SessionLocal()
    try:
        system_user = db.scalar(select(User).where(User.username == "system"))
        if system_user is None:
            system_user = User(username="system")
            db.add(system_user)
            db.commit()
            db.refresh(system_user)

        root = get_or_create_root(db, owner_id=system_user.id)
        if ("directories", "mode") in added_columns and root.mode != ROOT_MODE:
            root.mode = ROOT_MODE

        orphans = db.scalars(select(Block).where(~Block.replicas.any())).all()
        for block in orphans:
            db.add(
                BlockReplica(
                    block_pk=block.id,
                    datanode_id=block.datanode_id,
                    state=STORED if block.committed else PENDING,
                    checksum=block.checksum,
                    size_bytes=block.size_bytes,
                )
            )
        db.commit()
    finally:
        db.close()


def _replication_pass() -> None:
    db = SessionLocal()
    try:
        report = check_replication(db)
        db.commit()
        if report.scheduled or report.removed_excess or report.expired_tasks:
            logger.info(
                "Re-replicación: %d copias programadas, %d sobrantes quitadas, %d órdenes vencidas",
                report.scheduled,
                report.removed_excess,
                report.expired_tasks,
            )
    finally:
        db.close()


async def _replication_loop(stop: asyncio.Event) -> None:
    """Re-replicador (solo actúa en el líder)."""
    while not stop.is_set():
        try:
            if ha_state.is_leader:
                await run_in_threadpool(_replication_pass)
        except Exception:  # noqa: BLE001 - el bucle no debe morir
            logger.exception("Error en el re-replicador")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.replication_check_interval)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    added = prepare_database()
    changelog.install_hooks()
    async with httpx.AsyncClient(verify=settings.ca_cert or True) as client:
        await ha_manager.startup(client)
    if ha_state.is_leader:
        seed_and_migrate_data(added)

    stop = asyncio.Event()
    tasks = [asyncio.create_task(ha_manager.run(stop)), asyncio.create_task(_replication_loop(stop))]
    try:
        yield
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(
    title=settings.app_name,
    description=(
        "ControlNode del DFS por bloques tipo HDFS — Hito 3: namespace, "
        "placement y replicación de bloques, control de acceso y alta disponibilidad"
    ),
    version="0.3.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Traduce los errores de validación de FastAPI al formato ErrorResponse.

    Por defecto FastAPI devuelve un 422 con `detail` como lista de errores,
    que rompe el contrato de CONTRATOS.md (todo error debe tener la forma
    {"error": ..., "message": ...}) y deja al cliente sin un mensaje legible.
    """
    problems = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": ErrorResponse(
                error="validation_error",
                message=f"Petición inválida: {problems}" if problems else "Petición inválida",
            ).model_dump()
        },
    )


app.add_middleware(HAMiddleware)


@app.get("/health", tags=["infra"])
def health() -> dict[str, str]:
    """Endpoint de salud. Útil para healthchecks de Docker/orquestador."""
    return {
        "status": "ok",
        "service": settings.app_name,
        "controlnode_id": settings.controlnode_id,
        "role": ha_state.role,
    }


# ---------------------------------------------------------------------
# Routers de negocio. Las rutas de /fs son estáticas, así que no compiten
# con el catch-all /files/{path:path} y el orden de registro es indiferente.
#
# OJO con /files: el router de control (Hito 2) expone rutas con sufijo de
# acción sobre el mismo prefijo — /files/{path}/allocate|/confirm|/blocks —
# y el de transferencia reclama el catch-all /files/{path:path}. El de
# control DEBE registrarse ANTES para que, p. ej., `GET /files/a/blocks`
# resuelva al plan de lectura y no a "descargar el archivo /a/blocks".
# Ver el docstring de app/routers/control.py.
# ---------------------------------------------------------------------
from app.ha.router import router as ha_router  # noqa: E402
from app.routers.auth import router as auth_router  # noqa: E402
from app.routers.cluster import router as cluster_router  # noqa: E402
from app.routers.control import router as control_router  # noqa: E402
from app.routers.datanodes import router as datanodes_router  # noqa: E402
from app.routers.fs import router as fs_router  # noqa: E402
from app.routers.groups import router as groups_router  # noqa: E402
from app.routers.transfer import router as transfer_router  # noqa: E402

app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(fs_router, prefix="/fs", tags=["fs"])
app.include_router(groups_router, prefix="/groups", tags=["groups"])
app.include_router(cluster_router, prefix="/cluster", tags=["cluster"])
app.include_router(ha_router, prefix="/ha", tags=["ha"])
app.include_router(datanodes_router, prefix="/datanodes", tags=["datanodes"])
app.include_router(control_router, prefix="/files", tags=["control"])
app.include_router(transfer_router, prefix="/files", tags=["transfer"])
