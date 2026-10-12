"""
API ControlNode <-> ControlNode (prefix /ha).

- GET /ha/status    público: rol, época y líder (lo usan clientes,
                    DataNodes y los otros ControlNodes para encontrar al líder).
- GET /ha/log       (token de ControlNode) long-poll: entradas del log con
                    seq > `since`. El `since` que manda el respaldo es a la vez
                    su acuse: "ya apliqué hasta aquí" (replicación semi-síncrona).
- GET /ha/snapshot  (token de ControlNode) copia completa de los metadatos.
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from starlette.concurrency import run_in_threadpool

from app.cluster_auth import require_controlnode
from app.config import settings
from app.database import engine
from app.ha import changelog
from app.ha.state import state, status_dict

router = APIRouter()


def _not_leader() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "not_leader",
            "message": f"{settings.controlnode_id} no es el líder",
            "leader_id": state.leader_id,
        },
    )


@router.get("/status")
def ha_status() -> dict:
    if state.is_leader:
        # En el líder, "aplicado" es lo último que escribió.
        state.applied_seq = changelog.local_last_seq(engine)
    return status_dict()


def _read(since: int) -> tuple[list[dict], int, int]:
    with engine.connect() as conn:
        return changelog.read_entries(conn, since), changelog.first_seq(conn), changelog.last_seq(conn)


@router.get("/log")
async def ha_log(
    since: int = Query(ge=0),
    wait: float = Query(default=0.0, ge=0.0, le=30.0),
    follower: str = Depends(require_controlnode),
) -> dict:
    if not state.is_leader:
        raise _not_leader()
    state.record_follower(follower, since)
    deadline = time.monotonic() + wait
    while True:
        entries, first, last = await run_in_threadpool(_read, since)
        if since > last or (first and since < first - 1):
            # El respaldo va por delante (fue líder y no llegó a replicar) o
            # pide entradas ya podadas: necesita copia completa.
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={"error": "snapshot_required", "message": "Pide GET /ha/snapshot"},
            )
        if entries or time.monotonic() >= deadline or not state.is_leader:
            return {
                "epoch": state.epoch,
                "leader_id": settings.controlnode_id,
                "last_seq": last,
                "entries": entries,
            }
        await asyncio.sleep(0.05)


def _snapshot() -> dict:
    with engine.connect() as conn:
        with conn.begin():
            return changelog.export_snapshot(conn)


@router.get("/snapshot")
async def ha_snapshot(follower: str = Depends(require_controlnode)) -> dict:
    if not state.is_leader:
        raise _not_leader()
    snapshot = await run_in_threadpool(_snapshot)
    state.record_follower(follower, snapshot["seq"])
    return {**snapshot, "leader_id": settings.controlnode_id}
