"""Relación de este DataNode con los ControlNodes (Hito 3).

Con alta disponibilidad hay varios ControlNodes: un líder y respaldos. Este
módulo guarda lo que el DataNode sabe de ellos:

- Cuál es el líder (el que respondió `role=leader` con la época más alta) y
  la época máxima vista. Órdenes de un líder con época menor se ignoran
  (*fencing*: un líder viejo que no sabe que lo reemplazaron no puede
  mandar borrar nada).
- Cuándo fue el último contacto OK con cada ControlNode, por id. Se manda
  en cada heartbeat (`controlnodes`): es el informe de testigo que usa un
  respaldo para decidir si el líder cayó de verdad.
- Colas de informes que no se pudieron entregar (bloques recibidos,
  bloques corruptos, borrados ejecutados): salen en el próximo heartbeat.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import httpx

from app.config import settings
from app.security import node_headers

logger = logging.getLogger("datanode.cluster")


@dataclass
class ClusterState:
    leader_url: str | None = None
    leader_id: str | None = None
    max_epoch: int = 0
    last_ok: dict[str, float] = field(default_factory=dict)
    pending_received: dict[str, dict] = field(default_factory=dict)
    pending_corrupt: set[str] = field(default_factory=set)
    deleted: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def witness_view(self) -> dict[str, float]:
        now = time.monotonic()
        with self.lock:
            return {cn_id: round(now - seen, 3) for cn_id, seen in self.last_ok.items()}

    def mark_ok(self, controlnode_id: str) -> None:
        if controlnode_id:
            with self.lock:
                self.last_ok[controlnode_id] = time.monotonic()

    # -- colas -----------------------------------------------------------------
    def queue_received(self, reports: list[dict]) -> None:
        with self.lock:
            for report in reports:
                self.pending_received[report["block_id"]] = report

    def queue_corrupt(self, block_id: str) -> None:
        with self.lock:
            self.pending_corrupt.add(block_id)

    def take_received(self) -> list[dict]:
        with self.lock:
            return list(self.pending_received.values())

    def clear_received(self, reports: list[dict]) -> None:
        with self.lock:
            for report in reports:
                self.pending_received.pop(report["block_id"], None)


cluster = ClusterState()


def report_received_sync(reports: list[dict]) -> bool:
    """Informa al líder que este nodo guardó bloques. Si no hay líder
    conocido o falla, quedan en cola para el próximo heartbeat."""
    leader = cluster.leader_url
    if leader is None:
        cluster.queue_received(reports)
        return False
    try:
        resp = httpx.post(
            f"{leader}/datanodes/blocks/received",
            json={"node_id": settings.datanode_id, "blocks": reports},
            headers=node_headers(),
            timeout=5.0,
            verify=settings.verify,
        )
        if resp.status_code == 204:
            return True
        logger.warning("El ControlNode rechazó el informe de bloques: %s %s", resp.status_code, resp.text[:200])
    except httpx.HTTPError as exc:
        logger.warning("No se pudo informar al ControlNode: %s", exc)
    cluster.queue_received(reports)
    return False
