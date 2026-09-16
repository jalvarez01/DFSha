"""
Placement del ControlNode: liveness de DataNodes, particionamiento de
archivos en bloques y asignación de bloques a nodos (round-robin).

Este módulo es el "cerebro" del ControlNode en su rol tipo NameNode/HDFS:
no toca datos, solo decide *cómo* se reparte un archivo entre los DataNodes
vivos. Está separado de los routers para poder probar el algoritmo de
particionamiento de forma aislada (ver tests) y para que la lógica de
liveness viva en un solo sitio.

Algoritmo de asignación (RNF4 — particionamiento):
1. Se calcula el conjunto de DataNodes *vivos*: registrados y con un
   heartbeat reciente (dentro de `settings.heartbeat_ttl_seconds`).
2. El archivo se parte en ceil(size / block_size) bloques; todos miden
   `block_size` salvo el último, que lleva el resto.
3. Los bloques se reparten round-robin sobre los nodos vivos ordenados por
   id: el bloque i va al nodo `live[(start + i) % n]`. `start` rota entre
   llamadas (un cursor global) para que archivos pequeños no caigan siempre
   en el mismo nodo y la carga quede balanceada.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DataNode


class NoLiveDataNodesError(Exception):
    """No hay ningún DataNode vivo al que asignar bloques. El router lo
    traduce a 503 (el servicio de almacenamiento no está disponible)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: datetime | None) -> datetime | None:
    """Normaliza a UTC-aware. SQLite devuelve datetimes naive aunque la
    columna sea timezone=True; asumimos que todo lo que guardamos es UTC."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def is_alive(node: DataNode, now: datetime | None = None) -> bool:
    """True si el nodo está vivo: último estado 'alive' y heartbeat dentro
    de la ventana de gracia. La liveness es dinámica: un nodo que dejó de
    latir se considera muerto aunque su columna `status` siga en 'alive'."""
    now = now or _utcnow()
    if node.status != "alive":
        return False
    last = _as_utc(node.last_heartbeat)
    if last is None:
        return False
    return (now - last) <= timedelta(seconds=settings.heartbeat_ttl_seconds)


def live_datanodes(db: Session, now: datetime | None = None) -> list[DataNode]:
    """DataNodes vivos, ordenados por id (orden estable para el round-robin)."""
    now = now or _utcnow()
    nodes = db.scalars(select(DataNode).order_by(DataNode.id)).all()
    return [node for node in nodes if is_alive(node, now)]


# Cursor global del round-robin. Rota el nodo de arranque en cada allocate
# para repartir la carga entre archivos (no solo dentro de un archivo). Es
# best-effort: se reinicia al rearrancar el proceso, lo cual no afecta la
# corrección, solo el balanceo inicial. Protegido por un lock porque uvicorn
# puede atender requests en hilos distintos.
_cursor_lock = threading.Lock()
_cursor = 0


def _next_start(n: int) -> int:
    """Devuelve el índice de nodo de arranque y avanza el cursor."""
    global _cursor
    with _cursor_lock:
        start = _cursor % n
        _cursor = (_cursor + 1) % n
        return start


def reset_cursor() -> None:
    """Reinicia el cursor del round-robin. Solo para tests deterministas."""
    global _cursor
    with _cursor_lock:
        _cursor = 0


def new_block_id() -> str:
    """Id opaco y único para un bloque (clave en la API del DataNode)."""
    return uuid.uuid4().hex


def num_blocks(total_size: int, block_size: int) -> int:
    """ceil(total_size / block_size). Un archivo vacío no tiene bloques."""
    if total_size <= 0:
        return 0
    return (total_size + block_size - 1) // block_size


@dataclass
class BlockAssignment:
    """Resultado de planificar un bloque: su índice, tamaño y el DataNode
    al que le toca."""

    index: int
    size_bytes: int
    datanode: DataNode


def assign_one_node(live_nodes: list[DataNode]) -> DataNode:
    """Elige un único DataNode vivo (mismo cursor round-robin que
    plan_partition) para la escritura CRUD de un bloque puntual —
    POST /files/{path}/blocks/{index}/allocate. A diferencia de
    plan_partition, no depende del tamaño del bloque: sirve igual para
    reemplazar un bloque existente (tamaño > 0) que para uno vacío
    (tamaño 0, p. ej. al truncar).

    Lanza NoLiveDataNodesError si no hay ningún nodo vivo.
    """
    if not live_nodes:
        raise NoLiveDataNodesError("No hay DataNodes vivos para asignar el bloque")
    n = len(live_nodes)
    start = _next_start(n)
    return live_nodes[start]


def plan_partition(total_size: int, block_size: int, live_nodes: list[DataNode]) -> list[BlockAssignment]:
    """Parte un archivo de `total_size` en bloques de `block_size` y los
    asigna round-robin sobre `live_nodes`.

    Lanza NoLiveDataNodesError si hace falta al menos un bloque y no hay
    nodos vivos. Un archivo vacío devuelve una lista vacía sin exigir nodos.
    """
    count = num_blocks(total_size, block_size)
    if count == 0:
        return []
    if not live_nodes:
        raise NoLiveDataNodesError("No hay DataNodes vivos para asignar bloques")

    n = len(live_nodes)
    start = _next_start(n)
    assignments: list[BlockAssignment] = []
    remaining = total_size
    for i in range(count):
        size = min(block_size, remaining)
        remaining -= size
        node = live_nodes[(start + i) % n]
        assignments.append(BlockAssignment(index=i, size_bytes=size, datanode=node))
    return assignments
