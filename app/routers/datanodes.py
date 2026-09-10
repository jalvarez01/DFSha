"""
Router de control de DataNodes (Hito 2): registro y heartbeat.

Es la cara ControlNode <-> DataNode del sistema. Un DataNode (servicio de
bloques, implementado aparte en `datanode/`) hace:

    1. Al arrancar: POST /datanodes/register con su node_id, host y puerto.
    2. Periódicamente: POST /datanodes/heartbeat con su node_id.

El ControlNode con esto mantiene la tabla `datanodes` y sabe qué nodos
están vivos para repartir bloques (ver app/placement.py).

Nota de seguridad: en el Hito 2 estos dos endpoints van sin autenticación
de usuario (son tráfico interno nodo<->control dentro de la red privada del
servicio). El aseguramiento nodo-a-nodo (token compartido / mTLS) es del
Hito 3 (RNF6). Por eso NO dependen de get_current_user, a diferencia de los
endpoints de cliente en app/routers/control.py.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DataNode
from app.placement import is_alive
from app.schemas import (
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


@router.post("/register", response_model=DataNodeInfo)
def register_datanode(
    payload: DataNodeRegisterRequest,
    db: Session = Depends(get_db),
) -> DataNodeInfo:
    """Registra un DataNode (o lo reactiva si ya existía).

    Idempotente por `node_id`: si el DataNode se reinicia, vuelve a llamar
    con el mismo node_id y se actualiza su fila (host/puerto pueden cambiar)
    en lugar de duplicarla. El registro cuenta como un heartbeat: deja el
    nodo vivo de inmediato.
    """
    node = db.scalar(select(DataNode).where(DataNode.node_id == payload.node_id))
    now = _utcnow()

    if node is None:
        node = DataNode(
            node_id=payload.node_id,
            host=payload.host,
            port=payload.port,
            status="alive",
            last_heartbeat=now,
            registered_at=now,
        )
        db.add(node)
    else:
        node.host = payload.host
        node.port = payload.port
        node.status = "alive"
        node.last_heartbeat = now

    db.commit()
    db.refresh(node)
    return _to_info(node)


@router.post("/heartbeat", response_model=HeartbeatResponse)
def heartbeat(
    payload: HeartbeatRequest,
    db: Session = Depends(get_db),
) -> HeartbeatResponse:
    """Refresca el heartbeat de un DataNode ya registrado.

    Si el node_id no existe responde 404 `unknown_datanode`: el DataNode
    debe registrarse antes de latir (p. ej. tras un reinicio del ControlNode
    que perdió el estado, el DataNode reintenta register).
    """
    node = db.scalar(select(DataNode).where(DataNode.node_id == payload.node_id))
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorResponse(
                error="unknown_datanode",
                message=f"DataNode {payload.node_id!r} no registrado; llama antes a /datanodes/register",
            ).model_dump(),
        )

    node.last_heartbeat = _utcnow()
    node.status = "alive"
    db.commit()
    db.refresh(node)
    return HeartbeatResponse(
        node_id=node.node_id,
        status=DataNodeStatus.ALIVE,
        last_heartbeat=node.last_heartbeat,
    )


@router.get("", response_model=list[DataNodeInfo])
def list_datanodes(db: Session = Depends(get_db)) -> list[DataNodeInfo]:
    """Lista todos los DataNodes conocidos con su liveness calculada.

    Útil para el panel de operación y la demo del hito (ver qué nodos están
    arriba). No es parte estricta del flujo de datos, pero sale gratis.
    """
    nodes = db.scalars(select(DataNode).order_by(DataNode.id)).all()
    return [_to_info(node) for node in nodes]
