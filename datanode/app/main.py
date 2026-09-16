"""DataNode — servicio de almacenamiento de bloques (Hito 2).

Rol de Jacobo en la arquitectura C -> ControlNode (NM) -> DataNode del
boceto de equipo: un servicio nuevo e independiente del ControlNode, que
NUNCA ve el archivo completo (solo bloques sueltos) y con el que el
CLIENTE habla DIRECTO (el ControlNode solo entrega el plan con host:puerto
de cada bloque; los bytes nunca pasan por él — ver CONTRATOS.md).

Endpoints (sin autenticación de usuario: tráfico interno cliente<->nodo y
nodo<->control; el aseguramiento nodo-a-nodo es del Hito 3, RNF6):
- PUT    /blocks/{block_id}?file_id=...   sube/reemplaza un bloque
- GET    /blocks/{block_id}               descarga un bloque
- DELETE /blocks/{block_id}               borra un bloque suelto
- DELETE /files/{file_id}/blocks          borra TODOS los bloques de un
  archivo de un golpe (lo usa RF1 al propagar `rm` / `rmdir --recursive`)
- GET    /health
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response

from app.config import settings
from app.registration import register_loop
from app.storage import BlockNotFoundError, BlockStore

logging.basicConfig(level=logging.INFO)

store = BlockStore(settings.datanode_storage_dir)
_stop_event = asyncio.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(register_loop(_stop_event))
    try:
        yield
    finally:
        _stop_event.set()
        await task


app = FastAPI(title="DFSha DataNode", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "node_id": settings.datanode_id}


@app.put("/blocks/{block_id}")
async def put_block(block_id: str, request: Request, file_id: str) -> dict:
    """Sube (o reemplaza) el contenido de un bloque. `file_id` es
    obligatorio: es lo que le permite a este nodo organizar el storage como
    "archivo=directorio, bloque=archivo" en vez de un flat de bloques sin
    relación entre sí. El cliente lo obtiene de `AllocatedBlock.file_id`
    (respuesta de `POST /files/{path}/allocate` en el ControlNode)."""
    data = await request.body()
    size_bytes, checksum = store.put(block_id, file_id, data)
    return {"block_id": block_id, "file_id": file_id, "size_bytes": size_bytes, "checksum": checksum}


@app.get("/blocks/{block_id}")
def get_block(block_id: str) -> Response:
    try:
        data = store.get(block_id)
    except BlockNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": str(exc)}) from exc
    return Response(content=data, media_type="application/octet-stream")


@app.delete("/blocks/{block_id}")
def delete_block(block_id: str) -> dict:
    deleted = store.delete(block_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": block_id})
    return {"block_id": block_id, "deleted": True}


@app.delete("/files/{file_id}/blocks")
def delete_file_blocks(file_id: str) -> dict:
    """Borra de un golpe todos los bloques de un archivo. Lo llama RF1
    (rm / rmdir --recursive) en cada DataNode que tenga bloques de los
    archivos borrados, para que el borrado del namespace no deje bloques
    huérfanos ocupando disco en los DataNodes."""
    count = store.delete_file(file_id)
    return {"file_id": file_id, "blocks_deleted": count}
