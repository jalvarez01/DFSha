"""Registro y heartbeat de este DataNode contra el ControlNode.

Contrato consumido: POST /datanodes/register y POST /datanodes/heartbeat
(ver CONTRATOS.md, sección 4 — Hito 2). El registro es idempotente por
`node_id`, así que reintentar sin parar hasta que el ControlNode esté
arriba es seguro y es justo lo que hacemos: un DataNode puede arrancar
antes que el ControlNode (orden de arranque de docker-compose no
garantizado) y debe recuperarse solo.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger("datanode.registration")


async def register_loop(stop_event: asyncio.Event) -> None:
    """Registra este nodo y luego manda heartbeat cada
    `settings.heartbeat_interval` segundos hasta que `stop_event` se dispare.
    Nunca lanza: los errores de red solo se logean y se reintenta en el
    siguiente ciclo (el ControlNode marca el nodo como `dead` por TTL si
    deja de latir, que es exactamente el comportamiento deseado)."""
    body = {
        "node_id": settings.datanode_id,
        "host": settings.datanode_advertise_host,
        "port": settings.datanode_advertise_port,
    }
    registered = False
    async with httpx.AsyncClient(base_url=settings.controlnode_url, timeout=5.0) as client:
        while not stop_event.is_set():
            try:
                if not registered:
                    resp = await client.post("/datanodes/register", json=body)
                    resp.raise_for_status()
                    registered = True
                    logger.info("Registrado en ControlNode como %s", settings.datanode_id)
                else:
                    resp = await client.post(
                        "/datanodes/heartbeat", json={"node_id": settings.datanode_id}
                    )
                    if resp.status_code == 404:
                        # El ControlNode no nos conoce (reinició su BD, o
                        # es la primera vez): re-registrar en el próximo ciclo.
                        registered = False
                    else:
                        resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("No se pudo contactar al ControlNode: %s", exc)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.heartbeat_interval)
            except asyncio.TimeoutError:
                pass
