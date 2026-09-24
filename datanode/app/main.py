"""DataNode — servicio de almacenamiento de bloques (Hito 2).

Servicio nuevo e independiente del ControlNode, que NUNCA ve el archivo
completo (solo bloques sueltos) y con el que el CLIENTE habla DIRECTO: el
ControlNode solo entrega el plan con host:puerto de cada bloque; los bytes
nunca pasan por él (ver CONTRATOS.md).

Endpoints (todos salvo /health exigen `Authorization: Bearer <jwt>`, el
mismo token que emite el ControlNode; ver app/security.py):
- PUT    /blocks/{block_id}?file_id=...   sube/reemplaza un bloque
         (header opcional X-Checksum-Sha256: se rechaza si no coincide)
- GET    /blocks/{block_id}               descarga un bloque, verificado
         (header de respuesta X-Checksum-Sha256)
- DELETE /blocks/{block_id}               borra un bloque suelto
- DELETE /files/{file_id}/blocks          borra todos los bloques de un
                                          archivo (operación manual/admin)
- GET    /health

El borrado de bloques que el namespace ya no referencia (rm, rmdir -r,
reemplazos) NO llega por esta API: el ControlNode lo ordena en la respuesta
del heartbeat (ver app/registration.py).

Cuerpos de PUT y GET en streaming: el bloque nunca se carga entero en
memoria.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.registration import register_loop
from app.security import require_user
from app.storage import (
    BlockNotFoundError,
    BlockStore,
    ChecksumMismatchError,
    CorruptBlockError,
    InvalidIdError,
    validate_id,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("datanode")

store = BlockStore(settings.datanode_storage_dir)


def _delete_ordered_block(block_id: str) -> None:
    """Ejecuta una orden de borrado recibida en el heartbeat."""
    try:
        if store.delete(block_id):
            logger.info("Bloque %s borrado por orden del ControlNode", block_id)
    except InvalidIdError:
        logger.warning("Orden de borrado con block_id inválido ignorada: %r", block_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event = asyncio.Event()
    task = asyncio.create_task(register_loop(stop_event, on_delete=_delete_ordered_block))
    try:
        yield
    finally:
        stop_event.set()
        await task


app = FastAPI(title="DFSha DataNode", lifespan=lifespan)


def _error(status_code: int, error: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": error, "message": message})


def _valid(value: str, kind: str) -> str:
    try:
        return validate_id(value, kind)
    except InvalidIdError as exc:
        raise _error(400, "invalid_id", str(exc)) from exc


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "node_id": settings.datanode_id}


@app.put("/blocks/{block_id}")
async def put_block(
    block_id: str,
    request: Request,
    file_id: str,
    expected_checksum: str | None = Header(default=None, alias="X-Checksum-Sha256"),
    _user: str = Depends(require_user),
) -> dict:
    """Sube (o reemplaza) el contenido de un bloque, en streaming.

    `file_id` es obligatorio: permite organizar el storage como
    "archivo=directorio, bloque=archivo". El cliente lo obtiene de
    `AllocatedBlock.file_id` (respuesta de `allocate` en el ControlNode).
    Si llega `X-Checksum-Sha256`, el bloque se guarda solo si coincide
    (`400 checksum_mismatch` si no)."""
    _valid(block_id, "block_id")
    _valid(file_id, "file_id")
    pending = await run_in_threadpool(store.begin, block_id, file_id)
    try:
        async for chunk in request.stream():
            if chunk:
                await run_in_threadpool(pending.write, chunk)
    except BaseException:
        await run_in_threadpool(store.abort, pending)
        raise
    try:
        size_bytes, checksum = await run_in_threadpool(store.commit, pending, expected_checksum)
    except ChecksumMismatchError as exc:
        raise _error(400, "checksum_mismatch", f"Bloque {block_id} rechazado: {exc}") from exc
    return {"block_id": block_id, "file_id": file_id, "size_bytes": size_bytes, "checksum": checksum}


@app.get("/blocks/{block_id}")
def get_block(block_id: str, _user: str = Depends(require_user)) -> StreamingResponse:
    """Descarga un bloque en streaming, tras verificar que el contenido en
    disco coincide con el checksum con que se guardó."""
    _valid(block_id, "block_id")
    try:
        path, size, checksum = store.open_verified(block_id)
    except BlockNotFoundError as exc:
        raise _error(404, "not_found", f"Bloque {exc} no encontrado en {settings.datanode_id}") from exc
    except CorruptBlockError as exc:
        raise _error(500, "corrupt_block", str(exc)) from exc
    return StreamingResponse(
        store.iter_block(path),
        media_type="application/octet-stream",
        headers={"Content-Length": str(size), "X-Checksum-Sha256": checksum},
    )


@app.delete("/blocks/{block_id}")
def delete_block(block_id: str, _user: str = Depends(require_user)) -> dict:
    _valid(block_id, "block_id")
    if not store.delete(block_id):
        raise _error(404, "not_found", f"Bloque {block_id} no encontrado")
    return {"block_id": block_id, "deleted": True}


@app.delete("/files/{file_id}/blocks")
def delete_file_blocks(file_id: str, _user: str = Depends(require_user)) -> dict:
    """Borra de un golpe todos los bloques de un archivo en este nodo."""
    _valid(file_id, "file_id")
    count = store.delete_file(file_id)
    return {"file_id": file_id, "blocks_deleted": count}
