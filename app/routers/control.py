"""
Router de control de bloques (Hito 2): la cara Cliente <-> ControlNode del
DFS distribuido.

Aquí vive el flujo de dos fases con el que el cliente escribe y lee archivos
repartidos entre DataNodes, SIN que el ControlNode toque los datos:

- POST /files/{path}/allocate  -> plan de ESCRITURA
      El cliente dice cuánto pesa el archivo; el ControlNode lo parte en
      bloques, los asigna round-robin a DataNodes vivos (app/placement.py),
      persiste las filas `blocks` en estado `committed=False` y devuelve a
      qué host:puerto subir cada bloque.
- POST /files/{path}/confirm   -> cierre de la escritura
      Tras subir los bloques, el cliente confirma checksums y tamaños
      reales; el ControlNode marca los bloques `committed=True` y consolida
      el tamaño del archivo.
- GET  /files/{path}/blocks    -> plan de LECTURA
      Devuelve, en orden, de qué DataNode bajar cada bloque confirmado y con
      qué checksum verificarlo. El cliente descarga y reensambla.

Sobre las rutas y el enrutado
------------------------------
Estas rutas usan el convertidor `{path:path}` con un sufijo de acción
(/allocate, /confirm, /blocks). Como el router de transferencia
(app/routers/transfer.py) reclama el catch-all `/files/{path:path}` para
PUT/GET/HEAD, este router DEBE registrarse ANTES que aquel en app/main.py:
así una petición como `GET /files/a/b/blocks` cae aquí (plan de lectura de
`/a/b`) en vez de interpretarse como "descarga del archivo /a/b/blocks".
Consecuencia: `blocks`, `allocate` y `confirm` quedan reservados como último
segmento de una ruta bajo /files. No es una limitación real para nombres de
archivo normales.

Autenticación: a diferencia de register/heartbeat (tráfico interno), estos
endpoints son de cliente y exigen `get_current_user`, igual que el resto de
la API de archivos.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import Block, File as FileModel, User
from app.path_service import (
    AlreadyExistsError,
    InvalidPathError,
    NotADirectoryError,
    NotAFileError,
    NotFoundError,
    PathError,
    RootOperationError,
    find_child_directory,
    find_child_file,
    get_full_path,
    resolve_file,
    resolve_parent_and_name,
)
from app.placement import (
    NoLiveDataNodesError,
    live_datanodes,
    new_block_id,
    plan_partition,
)
from app.schemas import (
    AllocatedBlock,
    AllocateRequest,
    AllocateResponse,
    BlockPlacement,
    BlocksResponse,
    ConfirmRequest,
    ConfirmResponse,
    DataNodeLocation,
    ErrorResponse,
)

router = APIRouter()

# Reutiliza la misma traducción de excepciones de path_service -> HTTP que
# el resto de routers (tabla de CONTRATOS.md).
_ERROR_MAP: dict[type[PathError], tuple[int, str]] = {
    InvalidPathError: (400, "invalid_path"),
    NotFoundError: (404, "not_found"),
    NotADirectoryError: (409, "not_a_directory"),
    NotAFileError: (409, "not_a_file"),
    AlreadyExistsError: (409, "already_exists"),
    RootOperationError: (400, "root_operation"),
}


def _http_error(exc: PathError) -> HTTPException:
    status_code, error_code = _ERROR_MAP.get(type(exc), (400, "path_error"))
    return HTTPException(
        status_code=status_code,
        detail=ErrorResponse(error=error_code, message=str(exc)).model_dump(),
    )


def _as_absolute(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def _join_path(parent_path: str, leaf: str) -> str:
    return f"/{leaf}" if parent_path == "/" else f"{parent_path}/{leaf}"


def _full_path(db: Session, file_row: FileModel) -> str:
    return _join_path(get_full_path(db, file_row.parent), file_row.name)


def _location(node) -> DataNodeLocation:
    return DataNodeLocation(node_id=node.node_id, host=node.host, port=node.port)


@router.post("/{path:path}/allocate", response_model=AllocateResponse)
def allocate(
    path: str,
    payload: AllocateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AllocateResponse:
    """Fase 1 de la escritura: devuelve el plan de subida de un archivo.

    Crea (o reemplaza) la fila `File` y las filas `blocks` en estado
    pendiente, repartidas round-robin entre los DataNodes vivos. No recibe
    ni un byte de contenido: solo el tamaño total, con el que calcula el
    particionamiento.
    """
    path = _as_absolute(path)
    try:
        resolved = resolve_parent_and_name(db, path)
    except PathError as exc:
        raise _http_error(exc) from exc

    parent = resolved.parent
    leaf_name = resolved.leaf_name

    # No se puede "escribir" un archivo donde ya hay un directorio homónimo.
    if find_child_directory(db, parent.id, leaf_name) is not None:
        raise _http_error(
            NotADirectoryError(f"{path} ya existe como directorio, no se puede sobreescribir con un archivo")
        )

    block_size = payload.block_size or settings.block_size_bytes

    live = live_datanodes(db)
    try:
        assignments = plan_partition(payload.size_bytes, block_size, live)
    except NoLiveDataNodesError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorResponse(error="no_datanodes", message=str(exc)).model_dump(),
        ) from exc

    existing = find_child_file(db, parent.id, leaf_name)
    if existing is not None:
        file_row = existing
        # Reasignar: descartar el plan anterior. El borrado del contenido de
        # los bloques viejos en los DataNodes es tarea del RF1 distribuido;
        # aquí limpiamos los metadatos para no dejar bloques huérfanos.
        for old_block in list(file_row.blocks):
            db.delete(old_block)
        db.flush()
    else:
        file_row = FileModel(name=leaf_name, parent_id=parent.id, owner_id=user.id, size_bytes=0)
        db.add(file_row)
        db.flush()  # asigna file_row.id

    blocks_out: list[AllocatedBlock] = []
    for assignment in assignments:
        block_id = new_block_id()
        db.add(
            Block(
                block_id=block_id,
                file_id=file_row.id,
                block_index=assignment.index,
                size_bytes=assignment.size_bytes,
                datanode_id=assignment.datanode.id,
                committed=False,
            )
        )
        blocks_out.append(
            AllocatedBlock(
                index=assignment.index,
                block_id=block_id,
                size_bytes=assignment.size_bytes,
                datanode=_location(assignment.datanode),
            )
        )

    # Tamaño tentativo; se consolida en /confirm con lo realmente subido.
    file_row.size_bytes = payload.size_bytes
    db.commit()
    db.refresh(file_row)

    return AllocateResponse(
        path=_full_path(db, file_row),
        file_id=file_row.id,
        block_size=block_size,
        total_size=payload.size_bytes,
        blocks=blocks_out,
    )


@router.post("/{path:path}/confirm", response_model=ConfirmResponse)
def confirm(
    path: str,
    payload: ConfirmRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ConfirmResponse:
    """Fase 2 de la escritura: fija checksums/tamaños reales y marca los
    bloques como confirmados.

    Solo se pueden confirmar bloques que pertenezcan a este archivo y que
    hayan sido planificados por un /allocate previo. Tras confirmar, el
    tamaño del archivo se recalcula como la suma de sus bloques confirmados.
    """
    path = _as_absolute(path)
    try:
        file_row = resolve_file(db, path)
    except PathError as exc:
        raise _http_error(exc) from exc

    by_block_id = {block.block_id: block for block in file_row.blocks}
    for confirmation in payload.blocks:
        block = by_block_id.get(confirmation.block_id)
        if block is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ErrorResponse(
                    error="unknown_block",
                    message=f"El bloque {confirmation.block_id!r} no pertenece a {path}",
                ).model_dump(),
            )
        block.checksum = confirmation.checksum
        block.size_bytes = confirmation.size_bytes
        block.committed = True

    committed = [block for block in file_row.blocks if block.committed]
    file_row.size_bytes = sum(block.size_bytes for block in committed)
    # "Completo" = todos los bloques planificados quedaron confirmados. Un
    # archivo vacío (0 bloques) también cuenta como completo.
    complete = len(committed) == len(file_row.blocks)

    db.commit()
    db.refresh(file_row)

    return ConfirmResponse(
        path=_full_path(db, file_row),
        file_id=file_row.id,
        size_bytes=file_row.size_bytes,
        num_blocks=len(committed),
        complete=complete,
    )


@router.get("/{path:path}/blocks", response_model=BlocksResponse)
def get_blocks(
    path: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BlocksResponse:
    """Plan de lectura: de qué DataNode bajar cada bloque confirmado del
    archivo, en orden, con el checksum esperado para verificarlo.

    Solo devuelve bloques `committed`: un archivo a medio subir (allocate sin
    confirm) no expone bloques que aún no existen en los DataNodes.
    """
    path = _as_absolute(path)
    try:
        file_row = resolve_file(db, path)
    except PathError as exc:
        raise _http_error(exc) from exc

    placements: list[BlockPlacement] = []
    for block in file_row.blocks:  # ya vienen ordenados por block_index (relationship order_by)
        if not block.committed:
            continue
        placements.append(
            BlockPlacement(
                index=block.block_index,
                block_id=block.block_id,
                size_bytes=block.size_bytes,
                checksum=block.checksum,
                datanode=_location(block.datanode),
            )
        )

    return BlocksResponse(
        path=_full_path(db, file_row),
        file_id=file_row.id,
        size_bytes=sum(placement.size_bytes for placement in placements),
        num_blocks=len(placements),
        blocks=placements,
    )
