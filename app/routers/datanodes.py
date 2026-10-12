"""
Router de control de DataNodes: la cara ControlNode <-> DataNode.

Un DataNode (servicio de bloques, implementado aparte en `datanode/`) hace:

    1. Al arrancar: POST /datanodes/register con su node_id, direcciones
       (pública para clientes, interna para otros nodos) y esquema.
    2. Después: POST /datanodes/block-report con su inventario completo.
    3. Periódicamente: POST /datanodes/heartbeat. La respuesta trae las
       órdenes de borrado (`delete_blocks`) y de copia a otros DataNodes
       (`replicate`). El DataNode acusa los borrados en el heartbeat
       siguiente (`deleted_blocks`).
    4. Cada vez que guarda un bloque: POST /datanodes/blocks/received con
       el checksum que calculó (si falla, lo manda en el próximo heartbeat).

El ControlNode NUNCA llama a un DataNode: toda la comunicación la inicia el
DataNode, como en HDFS.

Seguridad (Hito 3): todos estos endpoints exigen un token de DataNode
firmado con el secreto del clúster (`require_datanode`), y el node_id del
token debe coincidir con el del cuerpo: un DataNode no puede hacerse pasar
por otro, y un usuario no puede hacerse pasar por un DataNode.

Alta disponibilidad: el DataNode late a TODOS los ControlNodes. El líder
procesa el heartbeat; un respaldo solo anota el informe de testigo
(`controlnodes`: cuánto hace que el DataNode habló con cada ControlNode, lo
que usa para decidir un failover) y responde sin órdenes.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.block_gc import acknowledge_and_collect
from app.cluster_auth import require_datanode
from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.ha.state import state as ha_state
from app.models import DataNode, User
from app.placement import is_alive
from app.replication import commands_for, reconcile_report, record_corrupt, record_received
from app.schemas import (
    BlockReportRequest,
    BlockReportResponse,
    BlocksReceivedRequest,
    DataNodeInfo,
    DataNodeRegisterRequest,
    DataNodeStatus,
    ErrorResponse,
    HeartbeatRequest,
    HeartbeatResponse,
)

router = APIRouter()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_info(node: DataNode) -> DataNodeInfo:
    """Serializa un DataNode calculando su liveness al vuelo."""
    effective = DataNodeStatus.ALIVE if is_alive(node) else DataNodeStatus.DEAD
    return DataNodeInfo(
        node_id=node.node_id,
        host=node.host,
        port=node.port,
        status=effective,
        last_heartbeat=node.last_heartbeat,
        registered_at=node.registered_at,
    )


def _check_identity(token_node_id: str, body_node_id: str) -> None:
    if token_node_id != body_node_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ErrorResponse(
                error="node_mismatch",
                message=f"El token es de {token_node_id!r} pero el cuerpo dice {body_node_id!r}",
            ).model_dump(),
        )


def _get_node(db: Session, node_id: str) -> DataNode:
    node = db.scalar(select(DataNode).where(DataNode.node_id == node_id))
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorResponse(
                error="unknown_datanode",
                message=f"DataNode {node_id!r} no registrado; llama antes a /datanodes/register",
            ).model_dump(),
        )
    return node


@router.post("/register", response_model=DataNodeInfo)
def register_datanode(
    payload: DataNodeRegisterRequest,
    db: Session = Depends(get_db),
    token_node: str = Depends(require_datanode),
) -> DataNodeInfo:
    """Registra un DataNode (o lo reactiva si ya existía).

    Idempotente por `node_id`: si el DataNode se reinicia, vuelve a llamar
    con el mismo node_id y se actualiza su fila (las direcciones pueden
    cambiar) en lugar de duplicarla. El registro cuenta como un heartbeat.
    """
    _check_identity(token_node, payload.node_id)
    node = db.scalar(select(DataNode).where(DataNode.node_id == payload.node_id))
    now = _utcnow()

    if node is None:
        node = DataNode(node_id=payload.node_id, registered_at=now)
        db.add(node)
    node.host = payload.host
    node.port = payload.port
    node.scheme = payload.scheme
    node.internal_host = payload.internal_host
    node.internal_port = payload.internal_port
    node.status = "alive"
    node.last_heartbeat = now

    db.commit()
    db.refresh(node)
    return _to_info(node)


@router.post("/heartbeat", response_model=HeartbeatResponse)
def heartbeat(
    payload: HeartbeatRequest,
    db: Session = Depends(get_db),
    token_node: str = Depends(require_datanode),
) -> HeartbeatResponse:
    """Refresca el heartbeat, procesa informes atrasados y acuses, y
    devuelve las órdenes de borrado y de copia pendientes.

    En un respaldo: solo anota el informe de testigo, sin tocar la BD.
    """
    _check_identity(token_node, payload.node_id)
    ha_state.record_witness(payload.node_id, payload.controlnodes)
    if not ha_state.is_leader:
        return HeartbeatResponse(
            node_id=payload.node_id,
            status=DataNodeStatus.ALIVE,
            last_heartbeat=_utcnow(),
            role=ha_state.role,
            epoch=ha_state.epoch,
            controlnode_id=settings.controlnode_id,
        )

    node = _get_node(db, payload.node_id)
    node.last_heartbeat = _utcnow()
    node.status = "alive"
    if payload.received_blocks:
        record_received(db, node, payload.received_blocks)
    if payload.corrupt_blocks:
        record_corrupt(db, node, payload.corrupt_blocks)
    # RF1 distribuido: el heartbeat es el canal por el que el DataNode se
    # entera de qué bloques borrar (ver app/block_gc.py) y, desde el Hito 3,
    # de qué bloques copiar a otros nodos (ver app/replication.py).
    to_delete = acknowledge_and_collect(db, node, payload.deleted_blocks)
    to_replicate = commands_for(db, node)
    db.commit()
    db.refresh(node)
    return HeartbeatResponse(
        node_id=node.node_id,
        status=DataNodeStatus.ALIVE,
        last_heartbeat=node.last_heartbeat,
        delete_blocks=to_delete,
        replicate=to_replicate,
        role=ha_state.role,
        epoch=ha_state.epoch,
        controlnode_id=settings.controlnode_id,
    )


@router.post("/blocks/received", status_code=204, response_model=None)
def blocks_received(
    payload: BlocksReceivedRequest,
    db: Session = Depends(get_db),
    token_node: str = Depends(require_datanode),
) -> None:
    """El DataNode informa que guardó bloques (de un cliente, de la cadena
    de réplica o de una re-replicación), con el checksum que calculó."""
    _check_identity(token_node, payload.node_id)
    node = _get_node(db, payload.node_id)
    record_received(db, node, payload.blocks)
    db.commit()


@router.post("/block-report", response_model=BlockReportResponse)
def block_report(
    payload: BlockReportRequest,
    db: Session = Depends(get_db),
    token_node: str = Depends(require_datanode),
) -> BlockReportResponse:
    """Inventario completo de un DataNode: se concilia con la tabla de
    réplicas (perdidas -> se re-replican; huérfanas -> se mandan borrar)."""
    _check_identity(token_node, payload.node_id)
    node = _get_node(db, payload.node_id)
    result = reconcile_report(db, node, payload.blocks)
    db.commit()
    return BlockReportResponse(known=result.known, added=result.added, lost=result.lost, orphans=result.orphans)


@router.get("", response_model=list[DataNodeInfo])
def list_datanodes(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[DataNodeInfo]:
    """Lista todos los DataNodes conocidos con su liveness calculada."""
    nodes = db.scalars(select(DataNode).order_by(DataNode.id)).all()
    return [_to_info(node) for node in nodes]
