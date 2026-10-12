"""
Estado del clúster (Hito 3): GET /cluster/status.

Resume lo que importa para operar y para la demo de alta disponibilidad:
rol y época del ControlNode, DataNodes vivos/caídos con cuántas réplicas
guarda cada uno, y bloques sub-replicados o perdidos.
"""
from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.ha.state import state as ha_state
from app.models import Block, BlockReplica, DataNode, File, PendingDeletion, ReplicationTask, User
from app.placement import is_alive
from app.replication import STORED, replication_stats
from app.schemas import ClusterNodeStatus, ClusterStatusResponse, ControlNodeStatus

router = APIRouter()


@router.get("/status", response_model=ClusterStatusResponse)
def cluster_status(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> ClusterStatusResponse:
    per_node = Counter(
        dict(
            db.execute(
                select(BlockReplica.datanode_id, func.count())
                .where(BlockReplica.state == STORED)
                .group_by(BlockReplica.datanode_id)
            ).all()
        )
    )
    nodes = [
        ClusterNodeStatus(
            node_id=node.node_id,
            address=f"{node.scheme}://{node.host}:{node.port}",
            status="alive" if is_alive(node) else "dead",
            blocks=per_node[node.id],
        )
        for node in db.scalars(select(DataNode).order_by(DataNode.node_id))
    ]
    under, missing = replication_stats(db)
    return ClusterStatusResponse(
        controlnode=ControlNodeStatus(
            controlnode_id=settings.controlnode_id,
            role=ha_state.role,
            epoch=ha_state.epoch,
            leader_id=ha_state.leader_id,
            applied_seq=ha_state.applied_seq,
            peers=[{"follower": fid, "acked_seq": seq} for fid, seq in ha_state.healthy_followers().items()],
        ),
        replication_factor=settings.replication_factor,
        datanodes=nodes,
        files=db.scalar(select(func.count()).select_from(File)) or 0,
        blocks=db.scalar(select(func.count()).select_from(Block).where(Block.committed.is_(True))) or 0,
        under_replicated=under,
        missing=missing,
        pending_replications=db.scalar(select(func.count()).select_from(ReplicationTask)) or 0,
        pending_deletions=db.scalar(select(func.count()).select_from(PendingDeletion)) or 0,
    )
