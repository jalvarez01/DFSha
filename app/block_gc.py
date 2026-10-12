"""
Recolección de bloques huérfanos (RF1 distribuido).

Cuando un bloque deja de pertenecer al namespace (rm, rmdir --recursive,
re-allocate de un archivo, reemplazo de un bloque, PUT heredado sobre un
archivo distribuido), hay que borrarlo también del disco de CADA DataNode
que tenga una réplica. Este módulo encola esas órdenes en
`pending_deletions` y las entrega a cada DataNode en la respuesta de su
heartbeat (ver app/routers/datanodes.py y el docstring de `PendingDeletion`
en app/models.py).

Regla de uso: llamar a `discard_blocks` ANTES de `db.commit()` y dentro de
la misma transacción que el cambio del namespace. Así nunca se pierde una
orden de borrado (si la transacción falla, tampoco se borró el metadato) y
nunca se borra un bloque que el namespace sigue referenciando.

Periodo de gracia (Hito 3, consistencia): la orden no se entrega hasta
pasados `deletion_grace_seconds`. Un lector que obtuvo el plan de lectura
justo antes del rm o del reemplazo todavía puede bajar el bloque viejo.

Todo se hace con el ORM (nunca con DELETE masivos de Core): así cada cambio
pasa por los hooks que alimentan la réplica de metadatos entre
ControlNodes (app/ha/changelog.py).
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Block, BlockReplica, DataNode, PendingDeletion, ReplicationTask

# Máximo de órdenes de borrado por heartbeat, para acotar el tamaño de la
# respuesta. Lo que no quepa sale en los heartbeats siguientes.
MAX_DELETIONS_PER_HEARTBEAT = 1000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def enqueue_deletion(
    db: Session, datanode_id: int, block_id: str, file_id: int, delay: float | None = None
) -> None:
    """Encola que `datanode_id` borre `block_id` (idempotente)."""
    grace = settings.deletion_grace_seconds if delay is None else delay
    not_before = _now() + timedelta(seconds=grace)
    existing = db.scalar(
        select(PendingDeletion).where(
            PendingDeletion.datanode_id == datanode_id, PendingDeletion.block_id == block_id
        )
    )
    if existing is None:
        db.add(PendingDeletion(datanode_id=datanode_id, block_id=block_id, file_id=file_id, not_before=not_before))


def discard_blocks(db: Session, blocks: Iterable[Block], delay: float | None = None) -> None:
    """Borra las filas `blocks` indicadas y encola el borrado físico de cada
    una de sus réplicas (también las pendientes: pudieron llegar a subirse).
    No hace commit."""
    blocks = list(blocks)
    if not blocks:
        return
    for block in blocks:
        nodes = {replica.datanode_id for replica in block.replicas} or {block.datanode_id}
        for datanode_id in nodes:
            enqueue_deletion(db, datanode_id, block.block_id, block.file_id, delay)
        for task in db.scalars(select(ReplicationTask).where(ReplicationTask.block_pk == block.id)):
            db.delete(task)
        db.delete(block)
    db.flush()


def discard_replica(db: Session, replica: BlockReplica, delay: float = 0.0) -> None:
    """Quita UNA réplica (perdida, corrupta o sobrante) y ordena borrarla de
    su DataNode. No hace commit."""
    block = replica.block
    enqueue_deletion(db, replica.datanode_id, block.block_id, block.file_id, delay)
    db.delete(replica)
    db.flush()


def discard_blocks_of_files(db: Session, file_ids: Iterable[int]) -> None:
    """Como `discard_blocks`, para todos los bloques de los archivos dados
    (confirmados o no)."""
    ids = list(file_ids)
    if not ids:
        return
    blocks = db.scalars(select(Block).where(Block.file_id.in_(ids))).all()
    discard_blocks(db, blocks)


def acknowledge_and_collect(db: Session, node: DataNode, deleted: Iterable[str]) -> list[str]:
    """Procesa un heartbeat: olvida las órdenes que el DataNode ya ejecutó
    (`deleted`) y devuelve las que le quedan pendientes y cuyo periodo de
    gracia ya pasó. No hace commit."""
    acked = set(deleted)
    if acked:
        for row in db.scalars(
            select(PendingDeletion).where(
                PendingDeletion.datanode_id == node.id, PendingDeletion.block_id.in_(acked)
            )
        ):
            db.delete(row)
        db.flush()
    now = _now()
    due: list[str] = []
    for row in db.scalars(
        select(PendingDeletion).where(PendingDeletion.datanode_id == node.id).order_by(PendingDeletion.id)
    ):
        if _as_utc(row.not_before) <= now:
            due.append(row.block_id)
            if len(due) >= MAX_DELETIONS_PER_HEARTBEAT:
                break
    return due
