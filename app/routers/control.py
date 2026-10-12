"""
Router de control de bloques: la cara Cliente <-> ControlNode del DFS
distribuido.

Aquí vive el flujo de dos fases con el que el cliente escribe y lee archivos
repartidos entre DataNodes, SIN que el ControlNode toque los datos:

- POST /files/{path}/allocate  -> plan de ESCRITURA
      El cliente dice cuánto pesa el archivo; el ControlNode lo parte en
      bloques y a cada uno le asigna una CADENA de réplica de
      `replication_factor` DataNodes vivos (app/placement.py). Persiste las
      filas `blocks` (`committed=False`) y sus réplicas pendientes, toma un
      lease de escritura exclusivo y devuelve, por bloque, la cadena y un
      token de escritura firmado.
- POST /files/{path}/confirm   -> cierre de la escritura
      Tras subir los bloques, el cliente confirma checksums y tamaños. El
      ControlNode solo acepta un bloque si al menos `min_replicas` DataNodes
      informaron haberlo guardado CON ESE MISMO checksum (no se fía del
      cliente), lo marca `committed=True` y libera el lease.
- GET  /files/{path}/blocks    -> plan de LECTURA
      Devuelve, en orden, las réplicas de cada bloque confirmado (vivas
      primero), el checksum y un token de lectura. Si una réplica falla, el
      cliente prueba la siguiente.
- POST /files/{path}/blocks/{i}/allocate -> reemplazar o anexar UN bloque.
- POST/DELETE /files/{path}/lock...      -> locks de archivo (RF3 lock()).

Hito 3: todas comprueban permisos (app/acl.py): leer exige `r`, escribir
`w`, crear un archivo `w+x` en el directorio padre.

Sobre las rutas y el enrutado
------------------------------
Estas rutas usan el convertidor `{path:path}` con un sufijo de acción
(/allocate, /confirm, /blocks, /lock, /locks). Como el router de
transferencia (app/routers/transfer.py) reclama el catch-all
`/files/{path:path}` para PUT/GET/HEAD, este router DEBE registrarse ANTES
que aquel en app/main.py: así una petición como `GET /files/a/b/blocks` cae
aquí (plan de lectura de `/a/b`) en vez de interpretarse como "descarga del
archivo /a/b/blocks". Consecuencia: esos sufijos quedan reservados como
último segmento de una ruta bajo /files.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import acl, locks
from app.block_gc import discard_blocks, discard_replica
from app.cluster_auth import create_block_token
from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import Block, BlockReplica, DataNode, File as FileModel, User
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
    assign_pipeline,
    is_alive,
    live_datanodes,
    new_block_id,
    plan_partition,
)
from app.replication import PENDING, STORED
from app.routers.transfer import delete_local_blobs
from app.schemas import (
    AllocatedBlock,
    AllocateRequest,
    AllocateResponse,
    BlockAllocateRequest,
    BlockPlacement,
    BlocksResponse,
    ConfirmRequest,
    ConfirmResponse,
    DataNodeLocation,
    ErrorResponse,
    LockRequest,
    LockResponse,
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


def _error(status_code: int, error: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=ErrorResponse(error=error, message=message).model_dump())


def _as_absolute(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def _join_path(parent_path: str, leaf: str) -> str:
    return f"/{leaf}" if parent_path == "/" else f"{parent_path}/{leaf}"


def _full_path(db: Session, file_row: FileModel) -> str:
    return _join_path(get_full_path(db, file_row.parent), file_row.name)


def _location(node: DataNode) -> DataNodeLocation:
    """Dirección PÚBLICA de un DataNode (la que usa el cliente)."""
    return DataNodeLocation(node_id=node.node_id, host=node.host, port=node.port, scheme=node.scheme)


def _resolve_file(db: Session, path: str) -> FileModel:
    try:
        return resolve_file(db, path)
    except PathError as exc:
        raise _http_error(exc) from exc


def _no_datanodes(exc: NoLiveDataNodesError) -> HTTPException:
    return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "no_datanodes", str(exc))


def _new_block(db: Session, file_row: FileModel, index: int, size: int, pipeline: list[DataNode]) -> Block:
    """Crea el bloque pendiente y una réplica pendiente por nodo de la cadena."""
    block = Block(
        block_id=new_block_id(),
        file_id=file_row.id,
        block_index=index,
        size_bytes=size,
        datanode_id=pipeline[0].id,
        committed=False,
    )
    db.add(block)
    db.flush()
    for node in pipeline:
        block.replicas.append(BlockReplica(block_pk=block.id, datanode_id=node.id, state=PENDING))
    db.flush()
    return block


def _allocated(block: Block, pipeline: list[DataNode], user: User, lease_id: str | None) -> AllocatedBlock:
    return AllocatedBlock(
        index=block.block_index,
        block_id=block.block_id,
        file_id=block.file_id,
        size_bytes=block.size_bytes,
        datanode=_location(pipeline[0]),
        datanodes=[_location(node) for node in pipeline],
        token=create_block_token(
            block_id=block.block_id, file_id=block.file_id, op="write", username=user.username, pipeline=pipeline
        ),
        lease_id=lease_id,
    )


# --------------------------------------------------------------------------
# Locks de archivo (RF3 lock()) — registradas antes que las rutas genéricas.
# --------------------------------------------------------------------------
def _lock_response(db: Session, lock, file_row: FileModel) -> LockResponse:
    return LockResponse(
        lock_id=lock.lock_id,
        path=_full_path(db, file_row),
        mode=lock.mode,
        owner=lock.owner.username,
        expires_at=lock.expires_at,
        implicit=lock.implicit,
    )


@router.post("/{path:path}/lock/{lock_id}/renew", response_model=LockResponse)
def renew_lock(
    path: str,
    lock_id: str,
    payload: LockRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> LockResponse:
    file_row = _resolve_file(db, _as_absolute(path))
    lock = locks.get_owned_lock(db, file_row, user, lock_id)
    locks.renew(lock, payload.ttl_seconds if payload else None)
    db.commit()
    db.refresh(lock)
    return _lock_response(db, lock, file_row)


@router.delete("/{path:path}/lock/{lock_id}", response_model=LockResponse)
def release_lock(path: str, lock_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    file_row = _resolve_file(db, _as_absolute(path))
    lock = locks.get_owned_lock(db, file_row, user, lock_id)
    response = _lock_response(db, lock, file_row)
    locks.release(db, lock)
    db.commit()
    return response


@router.post("/{path:path}/lock", response_model=LockResponse, status_code=201)
def acquire_lock(
    path: str, payload: LockRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> LockResponse:
    """Lock explícito: `exclusive` exige permiso de escritura, `shared` de lectura."""
    file_row = _resolve_file(db, _as_absolute(path))
    if payload.mode == locks.EXCLUSIVE:
        acl.require_write_file(db, user, file_row)
    else:
        acl.require_read_file(db, user, file_row)
    lock = locks.acquire(db, file_row, user, payload.mode, payload.ttl_seconds)
    db.commit()
    db.refresh(lock)
    return _lock_response(db, lock, file_row)


@router.get("/{path:path}/locks", response_model=list[LockResponse])
def list_locks(path: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    file_row = _resolve_file(db, _as_absolute(path))
    acl.require_traverse(db, user, file_row.parent)
    return [_lock_response(db, lock, file_row) for lock in locks.active_locks(db, file_row)]


# --------------------------------------------------------------------------
# Escritura
# --------------------------------------------------------------------------
@router.post("/{path:path}/blocks/{index}/allocate", response_model=AllocatedBlock)
def allocate_block(
    path: str,
    index: int,
    payload: BlockAllocateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AllocatedBlock:
    """Escritura CRUD de un único bloque (decisión de equipo: acceso CRUD,
    no WORM): actualiza un bloque ya existente o anexa uno nuevo al final
    del archivo, SIN tocar los demás bloques.

    - `index < número de bloques`: reemplaza ese bloque. La fila nueva
      queda pendiente (`committed=False`) y la vieja SIGUE confirmada y
      visible en el plan de lectura hasta que `confirm` confirme la nueva:
      un lector concurrente nunca ve un archivo con un hueco. Si ya había
      un reemplazo pendiente para ese índice, se descarta.
    - `index == número de bloques`: anexa un bloque nuevo al final (crecer
      el archivo). No se permite dejar huecos: un índice mayor es
      `400 invalid_block_index`.

    IMPORTANTE sobre el enrutado: esta ruta se registra ANTES que
    `/{path:path}/allocate` a propósito: `{path:path}` es greedy y
    `/files/foo/blocks/0/allocate` también calzaría con
    `{path:path}/allocate` (path="foo/blocks/0").

    El cliente sube el contenido por la cadena devuelta y cierra con el
    `/confirm` de siempre, mandando solo ese bloque.
    """
    path = _as_absolute(path)
    file_row = _resolve_file(db, path)
    acl.require_write_file(db, user, file_row)

    # Número de bloques = índices distintos (un índice puede tener una fila
    # confirmada y otra pendiente a la vez, ver el modelo Block).
    num_indexes = len({block.block_index for block in file_row.blocks})
    if index < 0 or index > num_indexes:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_block_index",
            f"Índice de bloque {index} inválido para {path} "
            f"(el archivo tiene {num_indexes} bloque(s); solo se puede "
            f"reemplazar 0..{num_indexes - 1} o anexar en {num_indexes})",
        )

    try:
        pipeline = assign_pipeline(live_datanodes(db))
    except NoLiveDataNodesError as exc:
        raise _no_datanodes(exc) from exc

    lease = locks.writer_lock(db, file_row, user, payload.lease_id)
    stale_pending = [b for b in file_row.blocks if b.block_index == index and not b.committed]
    discard_blocks(db, stale_pending, delay=0)
    block = _new_block(db, file_row, index, payload.size_bytes, pipeline)
    db.commit()
    db.refresh(block)
    return _allocated(block, pipeline, user, lease.lock_id)


@router.post("/{path:path}/allocate", response_model=AllocateResponse)
def allocate(
    path: str,
    payload: AllocateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AllocateResponse:
    """Fase 1 de la escritura: devuelve el plan de subida de un archivo.

    Crea (o reemplaza) la fila `File` y las filas `blocks` pendientes, cada
    una con su cadena de réplica. No recibe ni un byte de contenido: solo
    el tamaño total, con el que calcula el particionamiento.
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

    existing = find_child_file(db, parent.id, leaf_name)
    if existing is not None:
        acl.require_write_file(db, user, existing)
    else:
        acl.require_create(db, user, parent)

    block_size = payload.block_size or settings.block_size_bytes
    live = live_datanodes(db)
    try:
        assignments = plan_partition(payload.size_bytes, block_size, live)
    except NoLiveDataNodesError as exc:
        raise _no_datanodes(exc) from exc

    replaced_legacy_blob = False
    if existing is not None:
        file_row = existing
        lease = locks.writer_lock(db, file_row, user, payload.lease_id)
        # Reasignar: descartar el plan anterior. discard_blocks borra los
        # metadatos y encola el borrado físico de cada réplica (con periodo
        # de gracia, por si alguien está leyendo la versión anterior).
        discard_blocks(db, file_row.blocks)
        replaced_legacy_blob = True
    else:
        file_row = FileModel(name=leaf_name, parent_id=parent.id, owner_id=user.id, size_bytes=0)
        acl.apply_creation_defaults(file_row, parent)
        db.add(file_row)
        db.flush()  # asigna file_row.id
        lease = locks.writer_lock(db, file_row, user, None)

    blocks_out: list[AllocatedBlock] = []
    for assignment in assignments:
        block = _new_block(db, file_row, assignment.index, assignment.size_bytes, assignment.pipeline)
        blocks_out.append(_allocated(block, assignment.pipeline, user, lease.lock_id))

    # Tamaño tentativo; se consolida en /confirm con lo realmente subido.
    file_row.size_bytes = payload.size_bytes
    lease_id = lease.lock_id
    db.commit()
    db.refresh(file_row)
    if replaced_legacy_blob:
        # Si el archivo venía del PUT monolítico del Hito 1, su blob local
        # ya no representa el contenido: se borra tras el commit.
        delete_local_blobs([file_row.id])

    return AllocateResponse(
        path=_full_path(db, file_row),
        file_id=file_row.id,
        block_size=block_size,
        total_size=payload.size_bytes,
        blocks=blocks_out,
        lease_id=lease_id,
        replication=len(assignments[0].pipeline) if assignments else 0,
    )


def _confirm_lock(db: Session, file_row: FileModel, user: User, lease_id: str | None):
    """El lease con el que se confirma: el que manda el cliente o, si no
    manda ninguno (clientes del Hito 2), el que tenga sobre el archivo."""
    if lease_id:
        return locks.get_owned_lock(db, file_row, user, lease_id)
    own = None
    for lock in locks.active_locks(db, file_row):
        if lock.owner_id != user.id:
            raise locks.LockConflict(f"El archivo lo está escribiendo {lock.owner.username}")
        own = lock
    return own


@router.post("/{path:path}/confirm", response_model=ConfirmResponse)
def confirm(
    path: str,
    payload: ConfirmRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ConfirmResponse:
    """Fase 2 de la escritura: fija checksums/tamaños y marca los bloques
    como confirmados.

    Un bloque solo se confirma si al menos `min_replicas` DataNodes
    informaron haberlo guardado con el checksum que manda el cliente. Si no
    (aún no llegaron los informes, o el contenido no cuadra), responde
    `409 replicas_not_ready` sin cambiar nada; el cliente puede reintentar.
    """
    path = _as_absolute(path)
    file_row = _resolve_file(db, path)
    acl.require_write_file(db, user, file_row)
    lease = _confirm_lock(db, file_row, user, payload.lease_id)

    by_block_id = {block.block_id: block for block in file_row.blocks}
    # Primera pasada: validar todo antes de tocar nada.
    for confirmation in payload.blocks:
        block = by_block_id.get(confirmation.block_id)
        if block is None:
            raise _error(
                status.HTTP_400_BAD_REQUEST,
                "unknown_block",
                f"El bloque {confirmation.block_id!r} no pertenece a {path}",
            )
        if block.committed:
            continue
        good = [
            r
            for r in block.replicas
            if r.state == STORED and r.checksum == confirmation.checksum and r.size_bytes == confirmation.size_bytes
        ]
        if len(good) < settings.min_replicas:
            stored = [r for r in block.replicas if r.state == STORED]
            detail = (
                "ningún DataNode ha informado todavía haberlo guardado"
                if not stored
                else "el checksum o el tamaño no coinciden con lo que guardaron los DataNodes"
            )
            raise _error(
                status.HTTP_409_CONFLICT,
                "replicas_not_ready",
                f"Bloque {block.block_index} ({block.block_id}): {detail} "
                f"({len(good)}/{settings.min_replicas} réplicas válidas)",
            )

    # Segunda pasada: aplicar.
    for confirmation in payload.blocks:
        block = by_block_id[confirmation.block_id]
        if block.committed:
            continue
        # Si este bloque reemplaza a otro ya confirmado en el mismo índice,
        # el viejo sale del namespace ahora (con periodo de gracia para
        # lectores en curso). Flush antes de marcar el nuevo para no violar
        # la unicidad (file_id, índice, committed).
        superseded = [
            other for other in file_row.blocks if other.block_index == block.block_index and other.committed
        ]
        discard_blocks(db, superseded)
        for replica in list(block.replicas):
            if replica.state != STORED:
                # Nodo de la cadena que no informó haberlo guardado: sale del
                # plan y el re-replicador completa el factor. Sin orden de
                # borrado: si lo tenía y lo informa tarde, vuelve a contar.
                db.delete(replica)
            elif replica.checksum != confirmation.checksum:
                # Lo guardó con otro contenido: esa copia se manda borrar.
                discard_replica(db, replica)
        block.checksum = confirmation.checksum
        block.size_bytes = confirmation.size_bytes
        block.committed = True
        db.flush()
        db.expire(file_row, ["blocks"])  # recargar sin las filas descartadas

    committed = [block for block in file_row.blocks if block.committed]
    file_row.size_bytes = sum(block.size_bytes for block in committed)
    # "Completo" = no queda ningún bloque planificado sin confirmar. Un
    # archivo vacío (0 bloques) también cuenta como completo.
    complete = all(block.committed for block in file_row.blocks)
    if complete and lease is not None and lease.implicit:
        locks.release(db, lease)

    db.commit()
    db.refresh(file_row)

    return ConfirmResponse(
        path=_full_path(db, file_row),
        file_id=file_row.id,
        size_bytes=file_row.size_bytes,
        num_blocks=len(committed),
        complete=complete,
    )


# --------------------------------------------------------------------------
# Lectura
# --------------------------------------------------------------------------
@router.get("/{path:path}/blocks", response_model=BlocksResponse)
def get_blocks(
    path: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BlocksResponse:
    """Plan de lectura: réplicas de cada bloque confirmado del archivo, en
    orden, con el checksum esperado y un token de lectura.

    Solo devuelve bloques `committed`: un archivo a medio subir (allocate sin
    confirm) no expone bloques que aún no existen en los DataNodes. Las
    réplicas en nodos vivos van primero (rotadas por índice para repartir
    la carga de lectura); las de nodos caídos, al final.
    """
    path = _as_absolute(path)
    file_row = _resolve_file(db, path)
    acl.require_read_file(db, user, file_row)

    placements: list[BlockPlacement] = []
    for block in file_row.blocks:  # ya vienen ordenados por block_index (relationship order_by)
        if not block.committed:
            continue
        stored = [r for r in block.replicas if r.state == STORED]
        live = [r.datanode for r in stored if is_alive(r.datanode)]
        dead = [r.datanode for r in stored if not is_alive(r.datanode)]
        if live:
            shift = block.block_index % len(live)
            live = live[shift:] + live[:shift]
        nodes = live + dead or [block.datanode]
        placements.append(
            BlockPlacement(
                index=block.block_index,
                block_id=block.block_id,
                file_id=file_row.id,
                size_bytes=block.size_bytes,
                checksum=block.checksum,
                datanode=_location(nodes[0]),
                datanodes=[_location(node) for node in nodes],
                token=create_block_token(
                    block_id=block.block_id, file_id=file_row.id, op="read", username=user.username
                ),
            )
        )

    return BlocksResponse(
        path=_full_path(db, file_row),
        file_id=file_row.id,
        size_bytes=sum(placement.size_bytes for placement in placements),
        num_blocks=len(placements),
        blocks=placements,
    )
