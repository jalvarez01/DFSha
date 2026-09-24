"""
Punto de entrada de la aplicación FastAPI.

Este archivo SOLO se encarga de:
- Crear las tablas (create_all), migrar las de versiones anteriores y
  sembrar el directorio raíz al arrancar.
- Exponer /health.
- Registrar los routers (ver abajo; el orden de /files importa).

Los endpoints de negocio (ls/mkdir/rmdir/rm, put/get) NO se implementan
aquí: cada compañero debe crear su propio `APIRouter` en un módulo nuevo
(p. ej. app/routers/fs.py, app/routers/transfer.py) e incluirlo abajo con
`app.include_router(...)`, usando los modelos de app/schemas.py y las
funciones de app/path_service.py.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, inspect, select, text

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import BLOCKS_UNIQUE_CONSTRAINT, Block, User
from app.path_service import get_or_create_root
from app.schemas import ErrorResponse


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


def _seed_root_directory() -> None:
    """Crea las tablas si no existen y garantiza que exista:
    - un usuario 'system' (dueño inicial de la raíz), y
    - el directorio raíz ("/").

    Se ejecuta una sola vez al arrancar el servidor.
    """
    Base.metadata.create_all(bind=engine)
    _migrate_blocks_table(engine)

    db = SessionLocal()
    try:
        system_user = db.scalar(select(User).where(User.username == "system"))
        if system_user is None:
            system_user = User(username="system")
            db.add(system_user)
            db.commit()
            db.refresh(system_user)

        get_or_create_root(db, owner_id=system_user.id)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _seed_root_directory()
    yield
    # Nada que limpiar al apagar por ahora (SQLite no requiere pool teardown).


app = FastAPI(
    title=settings.app_name,
    description=(
        "ControlNode del DFS por bloques tipo HDFS — Hito 2: namespace, "
        "placement de bloques y registro de DataNodes"
    ),
    version="0.2.0",
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


@app.get("/health", tags=["infra"])
def health() -> dict[str, str]:
    """Endpoint de salud. Útil para healthchecks de Docker/orquestador."""
    return {"status": "ok", "service": settings.app_name}


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
from app.routers.auth import router as auth_router
from app.routers.control import router as control_router
from app.routers.datanodes import router as datanodes_router
from app.routers.fs import router as fs_router
from app.routers.transfer import router as transfer_router

app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(fs_router, prefix="/fs", tags=["fs"])
app.include_router(datanodes_router, prefix="/datanodes", tags=["datanodes"])
app.include_router(control_router, prefix="/files", tags=["control"])
app.include_router(transfer_router, prefix="/files", tags=["transfer"])
