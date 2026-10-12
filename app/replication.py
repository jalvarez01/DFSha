"""
Replicación de bloques (Hito 3: RNF2 alta disponibilidad, RNF3 consistencia).

Cada bloque se guarda en `replication_factor` DataNodes (3 por defecto).
Este módulo es la parte del ControlNode que sabe DÓNDE está cada réplica y
la mantiene al día:

1. Informes de los DataNodes (`record_received`): un DataNode avisa cada
   vez que guarda un bloque, con el checksum que él mismo calculó. Solo
   entonces la réplica pasa a `stored`. El ControlNode nunca se fía del
   cliente para saber dónde quedó un bloque.

2. Re-replicación (`check_replication`, cada `replication_check_interval`
   segundos en el líder): si un bloque confirmado tiene menos réplicas
   `stored` en nodos VIVOS que el factor (porque un nodo murió, o una
   réplica se perdió o se corrompió), se elige un origen vivo que lo tenga
   y un destino vivo que no, y se crea una `ReplicationTask`. La orden viaja
   al origen en la respuesta de su heartbeat (`commands_for`); el origen
   copia el bloque al destino (DataNode -> DataNode) y el destino lo
   informa como cualquier otro bloque recibido. Si la orden vence, se
   reprograma.

   Las réplicas de un nodo muerto NO se borran de la tabla: si el nodo
   vuelve, vuelven a contar, y las que sobren se eliminan.

3. Conciliación (`reconcile_report`): al arrancar (y periódicamente) cada
   DataNode manda su inventario completo. Las réplicas que el ControlNode
   creía en ese nodo y no aparecen se dan por perdidas (y se re-replican);
   los bloques que el nodo tiene y el namespace ya no referencia se mandan
   borrar.

Estrategia de colocación: el destino es el nodo vivo con menos réplicas
(balanceo), excluyendo los que ya tienen una réplica o una orden de borrado
pendiente de ese bloque.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.block_gc import discard_replica, enqueue_deletion
from app.cluster_auth import create_block_token
from app.config import settings
from app.models import Block, BlockReplica, DataNode, PendingDeletion, ReplicationTask
from app.placement import effective_replication, live_datanodes
from app.schemas import DataNodeLocation, ReceivedBlock, ReplicateCommand

logger = logging.getLogger("dfsha.replication")

STORED = "stored"
PENDING = "pending"
# Una orden enviada y no cumplida se reenvía pasado este tiempo.
RESEND_AFTER = timedelta(seconds=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _has_pending_deletion(db: Session, datanode_id: int, block_id: str) -> bool:
    return (
        db.scalar(
            select(PendingDeletion.id).where(
                PendingDeletion.datanode_id == datanode_id, PendingDeletion.block_id == block_id
            )
        )
        is not None
    )


def find_replica(block: Block, datanode_id: int) -> BlockReplica | None:
    for replica in block.replicas:
        if replica.datanode_id == datanode_id:
            return replica
    return None


# --------------------------------------------------------------------------
# 1. Informes de bloques recibidos
# --------------------------------------------------------------------------
def record_received(db: Session, node: DataNode, reports: list[ReceivedBlock]) -> None:
    """Marca como `stored` las réplicas que `node` informa haber guardado.
    No hace commit."""
    for report in reports:
        block = db.scalar(select(Block).where(Block.block_id == report.block_id))
        if block is None:
            # El namespace ya no lo referencia (borrado mientras se subía, o
            # escritura que nunca se confirmó antes de un failover): huérfano.
            if not _has_pending_deletion(db, node.id, report.block_id):
                enqueue_deletion(db, node.id, report.block_id, file_id=0, delay=0)
            continue
        if _has_pending_deletion(db, node.id, block.block_id):
            # Ya se ordenó borrar esta copia (perdida/corrupta/sobrante): no
            # se resucita.
            continue

        replica = find_replica(block, node.id)
        if replica is None:
            replica = BlockReplica(block_pk=block.id, datanode_id=node.id)
            db.add(replica)
            block.replicas.append(replica)
        replica.state = STORED
        replica.checksum = report.checksum
        replica.size_bytes = report.size_bytes

        if block.committed and block.checksum and block.checksum != report.checksum:
            logger.warning("Réplica corrupta de %s en %s: se descarta", block.block_id, node.node_id)
            discard_replica(db, replica)
            continue

        for task in db.scalars(
            select(ReplicationTask).where(ReplicationTask.block_pk == block.id, ReplicationTask.target_id == node.id)
        ):
            db.delete(task)
    db.flush()


def record_corrupt(db: Session, node: DataNode, block_ids: list[str]) -> None:
    """El DataNode detectó que su copia no cuadra con su checksum: se quita
    esa réplica (y se re-replica desde otra sana). No hace commit."""
    for block_id in block_ids:
        block = db.scalar(select(Block).where(Block.block_id == block_id))
        if block is None:
            continue
        replica = find_replica(block, node.id)
        if replica is not None:
            logger.warning("%s informa corrupta su réplica de %s", node.node_id, block_id)
            discard_replica(db, replica)


def stored_replicas(block: Block, live_ids: set[int] | None = None) -> list[BlockReplica]:
    return [
        replica
        for replica in block.replicas
        if replica.state == STORED and (live_ids is None or replica.datanode_id in live_ids)
    ]


# --------------------------------------------------------------------------
# 2. Re-replicación
# --------------------------------------------------------------------------
@dataclass
class ReplicationReport:
    scheduled: int = 0
    removed_excess: int = 0
    expired_tasks: int = 0
    under_replicated: int = 0
    missing: int = 0


def _replica_counts(db: Session) -> Counter:
    rows = db.execute(
        select(BlockReplica.datanode_id, func.count()).group_by(BlockReplica.datanode_id)
    ).all()
    return Counter({datanode_id: count for datanode_id, count in rows})


def _expire_tasks(db: Session, now: datetime) -> int:
    expired = 0
    limit = timedelta(seconds=settings.replication_task_timeout)
    for task in db.scalars(select(ReplicationTask)):
        if now - _as_utc(task.created_at) <= limit:
            continue
        replica = find_replica(task.block, task.target_id)
        if replica is not None and replica.state == PENDING:
            # Pudo quedar a medias en el destino: se ordena borrar.
            discard_replica(db, replica)
        db.delete(task)
        expired += 1
    db.flush()
    return expired


def check_replication(db: Session) -> ReplicationReport:
    """Una pasada del re-replicador. No hace commit."""
    report = ReplicationReport()
    now = _now()
    report.expired_tasks = _expire_tasks(db, now)

    live = live_datanodes(db, now)
    if not live:
        return report
    live_by_id = {node.id: node for node in live}
    target_count = effective_replication(len(live))
    load = _replica_counts(db)

    blocks = db.scalars(select(Block).where(Block.committed.is_(True)).order_by(Block.id)).all()
    for block in blocks:
        tasks = list(db.scalars(select(ReplicationTask).where(ReplicationTask.block_pk == block.id)))
        alive = stored_replicas(block, set(live_by_id))
        if not alive:
            report.missing += 1
            continue

        missing_copies = target_count - len(alive) - len(tasks)
        if len(alive) < target_count:
            report.under_replicated += 1

        if missing_copies > 0:
            occupied = {replica.datanode_id for replica in block.replicas}
            candidates = [
                node
                for node in live
                if node.id not in occupied and not _has_pending_deletion(db, node.id, block.block_id)
            ]
            candidates.sort(key=lambda node: (load[node.id], node.id))
            sources = sorted(alive, key=lambda replica: (load[replica.datanode_id], replica.datanode_id))
            for i, target in enumerate(candidates[:missing_copies]):
                source = sources[i % len(sources)]
                replica = BlockReplica(block_pk=block.id, datanode_id=target.id, state=PENDING)
                db.add(replica)
                block.replicas.append(replica)
                db.add(ReplicationTask(block_pk=block.id, source_id=source.datanode_id, target_id=target.id))
                load[target.id] += 1
                report.scheduled += 1
                logger.info(
                    "Re-replicando %s: %s -> %s",
                    block.block_id,
                    live_by_id[source.datanode_id].node_id,
                    target.node_id,
                )
        elif len(alive) > target_count and not tasks:
            # Sobran réplicas (p. ej. volvió un nodo que se había dado por
            # muerto): se quitan las de los nodos más cargados.
            extra = sorted(alive, key=lambda replica: (-load[replica.datanode_id], -replica.datanode_id))
            for replica in extra[: len(alive) - target_count]:
                load[replica.datanode_id] -= 1
                discard_replica(db, replica, delay=settings.deletion_grace_seconds)
                report.removed_excess += 1

        # Réplicas de la cadena de escritura que nunca se confirmaron.
        limit = timedelta(seconds=settings.replication_task_timeout)
        task_targets = {task.target_id for task in tasks}
        for replica in list(block.replicas):
            if (
                replica.state == PENDING
                and replica.created_at is not None  # None = creada en esta misma pasada
                and replica.datanode_id not in task_targets
                and now - _as_utc(replica.created_at) > limit
            ):
                discard_replica(db, replica)
    db.flush()
    return report


def commands_for(db: Session, node: DataNode) -> list[ReplicateCommand]:
    """Órdenes de copia para `node` (origen). Marca `sent_at`. No hace commit."""
    now = _now()
    commands: list[ReplicateCommand] = []
    for task in db.scalars(select(ReplicationTask).where(ReplicationTask.source_id == node.id)):
        if task.sent_at is not None and now - _as_utc(task.sent_at) < RESEND_AFTER:
            continue
        target = task.target
        token = create_block_token(
            block_id=task.block.block_id,
            file_id=task.block.file_id,
            op="write",
            username="replicator",
            pipeline=[target],
            ttl_seconds=int(settings.replication_task_timeout) + 60,
        )
        commands.append(
            ReplicateCommand(
                block_id=task.block.block_id,
                file_id=task.block.file_id,
                token=token,
                targets=[
                    DataNodeLocation(
                        node_id=target.node_id, host=target.peer_host, port=target.peer_port, scheme=target.scheme
                    )
                ],
            )
        )
        task.sent_at = now
    db.flush()
    return commands


# --------------------------------------------------------------------------
# 3. Conciliación con el inventario de un DataNode
# --------------------------------------------------------------------------
@dataclass
class ReconcileResult:
    known: int = 0
    added: int = 0
    lost: int = 0
    orphans: int = 0


def reconcile_report(db: Session, node: DataNode, reports: list[ReceivedBlock]) -> ReconcileResult:
    """No hace commit."""
    result = ReconcileResult()
    reported = {report.block_id: report for report in reports}

    for replica in list(db.scalars(select(BlockReplica).where(BlockReplica.datanode_id == node.id))):
        if replica.state != STORED:
            continue
        if replica.block.block_id not in reported:
            logger.warning("%s ya no tiene %s: réplica perdida", node.node_id, replica.block.block_id)
            db.delete(replica)
            result.lost += 1
    db.flush()

    known_reports: list[ReceivedBlock] = []
    for report in reports:
        block = db.scalar(select(Block).where(Block.block_id == report.block_id))
        if block is None:
            if not _has_pending_deletion(db, node.id, report.block_id):
                enqueue_deletion(db, node.id, report.block_id, file_id=0, delay=0)
                result.orphans += 1
            continue
        result.known += 1
        if find_replica(block, node.id) is None and not _has_pending_deletion(db, node.id, block.block_id):
            result.added += 1
        known_reports.append(report)
    record_received(db, node, known_reports)
    return result


# --------------------------------------------------------------------------
# Estado (para /cluster/status)
# --------------------------------------------------------------------------
def replication_stats(db: Session) -> tuple[int, int]:
    """(bloques con menos réplicas de las debidas, bloques sin ninguna réplica viva)."""
    live = live_datanodes(db)
    live_ids = {node.id for node in live}
    target = effective_replication(len(live)) if live else settings.replication_factor
    under = missing = 0
    for block in db.scalars(select(Block).where(Block.committed.is_(True))):
        alive = len(stored_replicas(block, live_ids))
        if alive == 0:
            missing += 1
        elif alive < target:
            under += 1
    return under, missing
