"""
Recolección de bloques huérfanos (RF1 distribuido).

Cuando un bloque deja de pertenecer al namespace (rm, rmdir --recursive,
re-allocate de un archivo, reemplazo de un bloque, PUT heredado sobre un
archivo distribuido), hay que borrarlo también del disco del DataNode que
lo guarda. Este módulo encola esas órdenes en `pending_deletions` y las
entrega a cada DataNode en la respuesta de su heartbeat (ver
app/routers/datanodes.py y el docstring de `PendingDeletion` en
app/models.py).

Regla de uso: llamar a `discard_blocks` ANTES de `db.commit()` y dentro de
la misma transacción que el cambio del namespace. Así nunca se pierde una
orden de borrado (si la transacción falla, tampoco se borró el metadato) y
nunca se borra un bloque que el namespace sigue referenciando.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Block, DataNode, PendingDeletion

# Máximo de órdenes de borrado por heartbeat, para acotar el tamaño de la
# respuesta. Lo que no quepa sale en los heartbeats siguientes.
MAX_DELETIONS_PER_HEARTBEAT = 1000


def discard_blocks(db: Session, blocks: Iterable[Block]) -> None:
    """Borra las filas `blocks` indicadas y encola su borrado físico en el
    DataNode correspondiente. No hace commit."""
    for block in list(blocks):
        already = db.scalar(
            select(PendingDeletion.id).where(
                PendingDeletion.datanode_id == block.datanode_id,
                PendingDeletion.block_id == block.block_id,
            )
        )
        if already is None:
            db.add(
                PendingDeletion(
                    datanode_id=block.datanode_id,
                    block_id=block.block_id,
                    file_id=block.file_id,
                )
            )
        db.delete(block)
    db.flush()


def discard_blocks_of_files(db: Session, file_ids: Iterable[int]) -> None:
    """Como `discard_blocks`, para todos los bloques de los archivos dados
    (confirmados o no: un bloque pendiente pudo llegar a subirse)."""
    ids = list(file_ids)
    if not ids:
        return
    blocks = db.scalars(select(Block).where(Block.file_id.in_(ids))).all()
    discard_blocks(db, blocks)


def acknowledge_and_collect(db: Session, node: DataNode, deleted: Iterable[str]) -> list[str]:
    """Procesa un heartbeat: olvida las órdenes que el DataNode ya ejecutó
    (`deleted`) y devuelve las que le quedan pendientes. No hace commit."""
    acked = list(deleted)
    if acked:
        db.execute(
            delete(PendingDeletion).where(
                PendingDeletion.datanode_id == node.id,
                PendingDeletion.block_id.in_(acked),
            )
        )
    return list(
        db.scalars(
            select(PendingDeletion.block_id)
            .where(PendingDeletion.datanode_id == node.id)
            .order_by(PendingDeletion.id)
            .limit(MAX_DELETIONS_PER_HEARTBEAT)
        )
    )
