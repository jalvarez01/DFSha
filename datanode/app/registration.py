"""Registro, heartbeat, inventario y órdenes de este DataNode.

Contrato consumido: POST /datanodes/register, /heartbeat, /block-report y
/blocks/received (ver CONTRATOS.md). Toda la comunicación la inicia el
DataNode y va con un token de nodo firmado con el secreto del clúster.

Ciclo (cada `heartbeat_interval` segundos):

1. Heartbeat a TODOS los ControlNodes (líder y respaldos). Lleva los
   informes atrasados (bloques recibidos, bloques corruptos), el acuse de
   los borrados ya hechos y el informe de testigo (cuánto hace que habló
   con cada ControlNode).
2. Líder = el que responde `role=leader` con la época más alta. Si un
   ControlNode responde 404 `unknown_datanode`, es el líder y no conoce a
   este nodo (primer arranque, o perdió su estado): se registra y le manda
   el inventario completo.
3. Se ejecutan las órdenes del líder: `delete_blocks` (se acusan en el
   heartbeat siguiente; hasta entonces el líder las repite, así que una
   orden no se pierde si el nodo cae a mitad) y `replicate` (copiar un
   bloque a otro DataNode, en segundo plano).

Nunca lanza: los errores de red se registran y se reintenta en el ciclo
siguiente. Un DataNode puede arrancar antes que los ControlNodes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import httpx

from app.cluster import cluster
from app.config import settings
from app.security import node_headers

logger = logging.getLogger("datanode.registration")


def _register_body() -> dict:
    return {
        "node_id": settings.datanode_id,
        "host": settings.datanode_advertise_host,
        "port": settings.datanode_advertise_port,
        "scheme": settings.scheme,
        "internal_host": settings.internal_host,
        "internal_port": settings.internal_port,
    }


async def _post(client: httpx.AsyncClient, url: str, path: str, body: dict):
    try:
        return await client.post(f"{url}{path}", json=body, headers=node_headers())
    except httpx.HTTPError as exc:
        logger.debug("Sin contacto con %s: %s", url, exc)
        return None


async def register_loop(
    stop_event: asyncio.Event,
    on_delete: Callable[[str], None] = lambda block_id: None,
    on_replicate: Callable[[dict], None] = lambda command: None,
    inventory: Callable[[], list[dict]] = list,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Bucle de heartbeat. `transport` solo existe para inyectar
    ControlNodes falsos en los tests."""
    urls = settings.controlnode_url_list
    last_report = 0.0
    report_due = True
    async with httpx.AsyncClient(timeout=5.0, transport=transport, verify=settings.verify) as client:
        while not stop_event.is_set():
            try:
                received = cluster.take_received()
                with cluster.lock:
                    corrupt = sorted(cluster.pending_corrupt)
                    acks = sorted(cluster.deleted)
                body = {
                    "node_id": settings.datanode_id,
                    "deleted_blocks": acks,
                    "received_blocks": received,
                    "corrupt_blocks": corrupt,
                    "controlnodes": cluster.witness_view(),
                }
                responses = await asyncio.gather(*(_post(client, url, "/datanodes/heartbeat", body) for url in urls))

                leader: tuple[str, dict] | None = None
                unknown_to: str | None = None
                for url, resp in zip(urls, responses):
                    if resp is None:
                        continue
                    if resp.status_code == 404:
                        unknown_to = url  # solo el líder responde 404
                        continue
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    cluster.mark_ok(data.get("controlnode_id", ""))
                    if data.get("role") == "leader" and (leader is None or data["epoch"] > leader[1]["epoch"]):
                        leader = (url, data)

                if leader is None and unknown_to is not None:
                    resp = await _post(client, unknown_to, "/datanodes/register", _register_body())
                    if resp is not None and resp.status_code == 200:
                        logger.info("Registrado en el ControlNode %s como %s", unknown_to, settings.datanode_id)
                        cluster.leader_url = unknown_to
                        report_due = True
                        continue  # heartbeat inmediato para recoger órdenes
                elif leader is not None:
                    url, data = leader
                    if data["epoch"] < cluster.max_epoch:
                        logger.warning(
                            "Ignoro al líder %s: época %d < %d ya vista", data.get("controlnode_id"), data["epoch"], cluster.max_epoch
                        )
                    else:
                        if data.get("controlnode_id") != cluster.leader_id:
                            logger.info("Líder: %s (época %d)", data.get("controlnode_id"), data["epoch"])
                            report_due = True
                        cluster.max_epoch = data["epoch"]
                        cluster.leader_url = url
                        cluster.leader_id = data.get("controlnode_id")
                        # El líder procesó los informes y acuses de este heartbeat.
                        cluster.clear_received(received)
                        with cluster.lock:
                            cluster.pending_corrupt.difference_update(corrupt)
                            cluster.deleted.difference_update(acks)
                        for block_id in data.get("delete_blocks", []):
                            try:
                                await asyncio.to_thread(on_delete, block_id)
                            except Exception:  # noqa: BLE001 - un disco con problemas no debe tumbar el latido
                                logger.exception("No se pudo borrar el bloque %s", block_id)
                                continue  # sin acuse: el líder la reenviará
                            with cluster.lock:
                                cluster.deleted.add(block_id)
                        for command in data.get("replicate", []):
                            on_replicate(command)
                else:
                    cluster.leader_url = None

                if cluster.leader_url and (report_due or time.monotonic() - last_report > settings.block_report_interval):
                    blocks = await asyncio.to_thread(inventory)
                    resp = await _post(
                        client,
                        cluster.leader_url,
                        "/datanodes/block-report",
                        {"node_id": settings.datanode_id, "blocks": blocks},
                    )
                    if resp is not None and resp.status_code == 200:
                        logger.info("Inventario enviado (%d bloques): %s", len(blocks), resp.json())
                        last_report = time.monotonic()
                        report_due = False
            except Exception:  # noqa: BLE001 - el bucle nunca debe morir
                logger.exception("Error en el ciclo de heartbeat")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.heartbeat_interval)
            except asyncio.TimeoutError:
                pass
