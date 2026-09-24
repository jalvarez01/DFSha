"""Registro, heartbeat y órdenes de borrado de este DataNode.

Contrato consumido: POST /datanodes/register y POST /datanodes/heartbeat
(ver CONTRATOS.md, sección 4 — Hito 2). El registro es idempotente por
`node_id`, así que reintentar sin parar hasta que el ControlNode esté
arriba es seguro y es justo lo que hacemos: un DataNode puede arrancar
antes que el ControlNode (orden de arranque de docker-compose no
garantizado) y debe recuperarse solo.

RF1 distribuido: la respuesta del heartbeat trae `delete_blocks`, los
bloques que el namespace ya no referencia (rm, rmdir -r, reemplazos). Este
nodo los borra y los acusa en el heartbeat siguiente (`deleted_blocks`);
hasta recibir el acuse, el ControlNode los vuelve a mandar, así que una
orden no se pierde si el nodo se cae a mitad.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import httpx

from app.config import settings

logger = logging.getLogger("datanode.registration")


async def register_loop(
    stop_event: asyncio.Event,
    on_delete: Callable[[str], None] = lambda block_id: None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Registra este nodo y luego manda heartbeat cada
    `settings.heartbeat_interval` segundos hasta que `stop_event` se dispare.
    Ejecuta con `on_delete` cada orden de borrado recibida. `transport`
    solo existe para inyectar un ControlNode falso en los tests.

    Nunca lanza: los errores de red solo se logean y se reintenta en el
    siguiente ciclo (el ControlNode marca el nodo como `dead` por TTL si
    deja de latir, que es exactamente el comportamiento deseado)."""
    body = {
        "node_id": settings.datanode_id,
        "host": settings.datanode_advertise_host,
        "port": settings.datanode_advertise_port,
    }
    registered = False
    # Bloques ya borrados cuyo acuse aún no recibió el ControlNode.
    to_acknowledge: set[str] = set()
    async with httpx.AsyncClient(
        base_url=settings.controlnode_url, timeout=5.0, transport=transport
    ) as client:
        while not stop_event.is_set():
            try:
                if not registered:
                    resp = await client.post("/datanodes/register", json=body)
                    resp.raise_for_status()
                    registered = True
                    logger.info("Registrado en ControlNode como %s", settings.datanode_id)
                    # Primer heartbeat inmediato: recoge órdenes pendientes
                    # (p. ej. borrados ocurridos mientras el nodo estaba caído).
                    continue

                sent_acks = sorted(to_acknowledge)
                resp = await client.post(
                    "/datanodes/heartbeat",
                    json={"node_id": settings.datanode_id, "deleted_blocks": sent_acks},
                )
                if resp.status_code == 404:
                    # El ControlNode no nos conoce (reinició su BD, o es la
                    # primera vez): re-registrar en el próximo ciclo.
                    registered = False
                else:
                    resp.raise_for_status()
                    to_acknowledge.difference_update(sent_acks)
                    for block_id in resp.json().get("delete_blocks", []):
                        try:
                            await asyncio.to_thread(on_delete, block_id)
                        except Exception:  # noqa: BLE001 - un disco con problemas no debe tumbar el latido
                            # Sin acuse: el ControlNode la reenviará.
                            logger.exception("No se pudo borrar el bloque %s", block_id)
                            continue
                        to_acknowledge.add(block_id)
            except httpx.HTTPError as exc:
                logger.warning("No se pudo contactar al ControlNode: %s", exc)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.heartbeat_interval)
            except asyncio.TimeoutError:
                pass
