"""DataNode — servicio de almacenamiento de bloques.

Servicio independiente del ControlNode, que NUNCA ve el archivo completo
(solo bloques sueltos) y con el que el CLIENTE habla DIRECTO: el ControlNode
entrega el plan con las direcciones de cada bloque; los bytes nunca pasan
por él (ver CONTRATOS.md).

Endpoints:
- PUT    /blocks/{block_id}?file_id=...   sube un bloque (token de bloque `write`)
         Header opcional X-Checksum-Sha256: se rechaza si no coincide.
         Tras guardarlo, lo informa al ControlNode y lo REENVÍA al siguiente
         DataNode de la cadena de réplica que trae el token (Hito 3).
- GET    /blocks/{block_id}               descarga un bloque (token `read`),
         verificado contra su checksum (header X-Checksum-Sha256).
- DELETE /blocks/{block_id}               borra un bloque (token de ControlNode)
- DELETE /files/{file_id}/blocks          borra los bloques de un archivo (ídem)
- GET    /health

Los borrados normales (rm, rmdir -r, reemplazos) y las copias para
re-replicar llegan como órdenes en la respuesta del heartbeat (ver
app/registration.py), no por esta API.

Cuerpos de PUT y GET en streaming: el bloque nunca se carga entero en
memoria. Con clave configurada, los bloques se guardan cifrados (AES-GCM).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.cluster import cluster, report_received_sync
from app.config import settings
from app.registration import register_loop
from app.security import block_token, require_controlnode
from app.storage import (
    BlockExistsError,
    BlockNotFoundError,
    BlockStore,
    ChecksumMismatchError,
    CorruptBlockError,
    InvalidIdError,
    load_key,
    validate_id,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("datanode")

store = BlockStore(settings.datanode_storage_dir, load_key(settings.datanode_storage_key))

# Copias de re-replicación en curso (block_id, destino): una orden repetida
# mientras la anterior sigue en marcha no lanza otra.
_replicating: set[tuple[str, str]] = set()
_replicating_lock = threading.Lock()


def _delete_ordered_block(block_id: str) -> None:
    """Ejecuta una orden de borrado recibida en el heartbeat."""
    try:
        if store.delete(block_id):
            logger.info("Bloque %s borrado por orden del ControlNode", block_id)
    except InvalidIdError:
        logger.warning("Orden de borrado con block_id inválido ignorada: %r", block_id)


def _url(location: dict) -> str:
    return f"{location.get('scheme', 'http')}://{location['host']}:{location['port']}"


def _push_block(block_id: str, file_id: str, token: str, target: dict) -> dict:
    """Envía la copia local de un bloque a otro DataNode (DataNode ->
    DataNode). Devuelve la respuesta del destino."""
    path, size, checksum = store.open_verified(block_id)
    resp = httpx.put(
        f"{_url(target)}/blocks/{block_id}",
        params={"file_id": file_id},
        content=store.iter_block(path),
        headers={
            "Authorization": f"Bearer {token}",
            "X-Checksum-Sha256": checksum,
            "Content-Length": str(size),
        },
        timeout=60.0,
        verify=settings.verify,
    )
    resp.raise_for_status()
    return resp.json()


def _replicate(command: dict) -> None:
    """Orden de re-replicación: copiar este bloque a cada destino."""
    block_id = command["block_id"]
    for target in command["targets"]:
        key = (block_id, target["node_id"])
        with _replicating_lock:
            if key in _replicating:
                continue
            _replicating.add(key)
        try:
            _push_block(block_id, str(command["file_id"]), command["token"], target)
            logger.info("Bloque %s copiado a %s", block_id, target["node_id"])
        except CorruptBlockError:
            logger.warning("No copio %s: mi copia está corrupta", block_id)
            cluster.queue_corrupt(block_id)
        except (BlockNotFoundError, httpx.HTTPError) as exc:
            logger.warning("No se pudo copiar %s a %s: %s", block_id, target["node_id"], exc)
        finally:
            with _replicating_lock:
                _replicating.discard(key)


def _start_replication(command: dict) -> None:
    threading.Thread(target=_replicate, args=(command,), daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        register_loop(
            stop_event,
            on_delete=_delete_ordered_block,
            on_replicate=_start_replication,
            inventory=store.inventory,
        )
    )
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
    return {
        "status": "ok",
        "node_id": settings.datanode_id,
        "leader": cluster.leader_id,
        "encrypted_at_rest": store.encrypted,
    }


def _forward(block_id: str, file_id: str, token: str, payload: dict) -> tuple[list[str], list[str]]:
    """Cadena de réplica: reenvía el bloque al siguiente nodo después de
    este. Si ese falla, prueba el que sigue (que a su vez reenvía a los de
    detrás). Devuelve (nodos que lo guardaron, nodos que fallaron)."""
    pipe = payload.get("pipe") or []
    ids = [entry["node_id"] for entry in pipe]
    downstream = pipe[ids.index(settings.datanode_id) + 1 :]
    failed: list[str] = []
    for target in downstream:
        try:
            body = _push_block(block_id, file_id, token, target)
            return body.get("stored_on", [target["node_id"]]), failed + body.get("failed", [])
        except (httpx.HTTPError, BlockNotFoundError, CorruptBlockError) as exc:
            logger.warning("Cadena de réplica: %s no recibió %s (%s)", target["node_id"], block_id, exc)
            failed.append(target["node_id"])
    return [], failed


@app.put("/blocks/{block_id}")
async def put_block(
    block_id: str,
    request: Request,
    file_id: str,
    expected_checksum: str | None = Header(default=None, alias="X-Checksum-Sha256"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict:
    """Sube un bloque en streaming y lo reenvía por la cadena de réplica.

    `file_id` es obligatorio: permite organizar el storage como
    "archivo=directorio, bloque=archivo". Si llega `X-Checksum-Sha256`, el
    bloque se guarda solo si coincide (`400 checksum_mismatch`). Un bloque
    ya guardado con otro contenido da `409 block_exists` (son inmutables).
    """
    _valid(block_id, "block_id")
    _valid(file_id, "file_id")
    payload = block_token(authorization, block_id, "write", file_id)

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
    except BlockExistsError as exc:
        raise _error(409, "block_exists", str(exc)) from exc

    report = {"block_id": block_id, "checksum": checksum, "size_bytes": size_bytes}
    await run_in_threadpool(report_received_sync, [report])
    token = authorization.partition(" ")[2].strip()
    downstream, failed = await run_in_threadpool(_forward, block_id, file_id, token, payload)
    return {
        "block_id": block_id,
        "file_id": file_id,
        "size_bytes": size_bytes,
        "checksum": checksum,
        "stored_on": [settings.datanode_id, *downstream],
        "failed": failed,
    }


@app.get("/blocks/{block_id}")
def get_block(block_id: str, authorization: str | None = Header(default=None, alias="Authorization")) -> StreamingResponse:
    """Descarga un bloque en streaming, tras verificar que el contenido en
    disco coincide con el checksum con que se guardó."""
    _valid(block_id, "block_id")
    block_token(authorization, block_id, "read")
    try:
        path, size, checksum = store.open_verified(block_id)
    except BlockNotFoundError as exc:
        raise _error(404, "not_found", f"Bloque {exc} no encontrado en {settings.datanode_id}") from exc
    except CorruptBlockError as exc:
        cluster.queue_corrupt(block_id)
        raise _error(500, "corrupt_block", str(exc)) from exc
    return StreamingResponse(
        store.iter_block(path),
        media_type="application/octet-stream",
        headers={"Content-Length": str(size), "X-Checksum-Sha256": checksum},
    )


@app.delete("/blocks/{block_id}")
def delete_block(block_id: str, _cn: str = Depends(require_controlnode)) -> dict:
    _valid(block_id, "block_id")
    if not store.delete(block_id):
        raise _error(404, "not_found", f"Bloque {block_id} no encontrado")
    return {"block_id": block_id, "deleted": True}


@app.delete("/files/{file_id}/blocks")
def delete_file_blocks(file_id: str, _cn: str = Depends(require_controlnode)) -> dict:
    """Borra de un golpe todos los bloques de un archivo en este nodo."""
    _valid(file_id, "file_id")
    count = store.delete_file(file_id)
    return {"file_id": file_id, "blocks_deleted": count}
