"""
Ciclo de vida de HA de un ControlNode: arranque, seguimiento del líder,
failover y degradación.

Arranque (`startup`)
    Se consulta /ha/status de los pares. Si alguno ya es líder, este nodo
    arranca como respaldo y se sincroniza con un snapshot completo. Si no
    hay líder y este nodo está configurado como `primary`, se hace líder
    con una época nueva. Un `standby` sin líder espera.

Respaldo (`_follow_once`)
    Long-poll a GET {líder}/ha/log?since=<último aplicado>: aplica las
    entradas en orden, en una transacción. El `since` del pedido siguiente
    confirma lo aplicado (el líder espera esa confirmación antes de
    responder a sus clientes: replicación semi-síncrona).

Failover (`_maybe_promote`)
    Un respaldo se promueve si, durante `ha_failover_seconds`:
    1. no logra hablar con el líder, y ningún par responde como líder;
    2. los DataNodes (testigos) tampoco lo ven: entre los que le han
       informado en los últimos 30 s, menos de la mitad tuvo contacto
       reciente con el líder. Así un respaldo aislado por una partición de
       red no se promueve mientras el líder sigue atendiendo al clúster
       (evita dos líderes);
    3. si hay varios respaldos, ninguno alcanzable tiene más log aplicado
       (o lo mismo y un id menor).
    Al promoverse sube la época.

Degradación (`_check_peers_as_leader`)
    Un líder que descubre otro líder con época mayor (p. ej. el primario
    viejo que vuelve tras un failover) pasa a respaldo y se resincroniza con
    un snapshot: descarta lo que no llegó a replicar.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
from starlette.concurrency import run_in_threadpool

from app.cluster_auth import node_auth_headers
from app.config import settings
from app.database import SessionLocal, engine
from app.ha import changelog
from app.ha.state import LEADER, STANDBY, load_epoch, save_epoch, state

logger = logging.getLogger("dfsha.ha")

PEER_CHECK_INTERVAL = 2.0
PRUNE_INTERVAL = 60.0


def _verify():
    return settings.ca_cert or True


def _headers() -> dict[str, str]:
    return node_auth_headers(settings.controlnode_id, "controlnode")


async def _peer_statuses(client: httpx.AsyncClient) -> list[tuple[str, dict]]:
    """[(url, status)] de los pares que responden."""

    async def one(url: str):
        try:
            resp = await client.get(f"{url}/ha/status", timeout=2.0)
            if resp.status_code == 200:
                return url, resp.json()
        except httpx.HTTPError:
            pass
        return None

    results = await asyncio.gather(*(one(url) for url in settings.ha_peer_urls))
    return [result for result in results if result is not None]


def _become_leader(epoch: int) -> None:
    state.epoch = epoch
    state.role = LEADER
    state.leader_id = settings.controlnode_id
    state.leader_url = None
    state.need_snapshot = False
    save_epoch(epoch)
    logger.warning("%s es ahora LÍDER (época %d)", settings.controlnode_id, epoch)


def _become_standby(leader_url: str | None, leader_id: str | None, epoch: int, snapshot: bool = True) -> None:
    state.role = STANDBY
    state.leader_url = leader_url
    state.leader_id = leader_id
    if epoch > state.epoch:
        state.epoch = epoch
        save_epoch(epoch)
    state.need_snapshot = snapshot
    state.last_leader_contact = time.monotonic()
    logger.warning("%s es RESPALDO de %s (época %d)", settings.controlnode_id, leader_id, state.epoch)


async def startup(client: httpx.AsyncClient) -> None:
    """Decide el rol inicial. Llamar antes de atender peticiones."""
    state.enabled = bool(settings.ha_peer_urls)
    db = SessionLocal()
    try:
        stored_epoch = load_epoch(db)
    finally:
        db.close()
    state.applied_seq = changelog.local_last_seq(engine)

    if not state.enabled:
        state.role = LEADER
        state.epoch = max(stored_epoch, 1)
        state.leader_id = settings.controlnode_id
        return

    peers = await _peer_statuses(client)
    leaders = [(url, st) for url, st in peers if st.get("role") == LEADER]
    known_epoch = max([stored_epoch] + [int(st.get("epoch", 0)) for _, st in peers])
    if leaders:
        url, st = max(leaders, key=lambda item: int(item[1].get("epoch", 0)))
        state.epoch = stored_epoch
        _become_standby(url, st.get("controlnode_id"), int(st.get("epoch", 0)))
    elif settings.ha_initial_role == "primary":
        _become_leader(known_epoch + 1)
    else:
        state.epoch = max(stored_epoch, 1)
        _become_standby(None, None, state.epoch)


# --------------------------------------------------------------------------
# Respaldo
# --------------------------------------------------------------------------
def _apply(entries: list[dict]) -> int:
    with engine.begin() as conn:
        return changelog.apply_entries(conn, entries)


def _import(snapshot: dict) -> int:
    with engine.begin() as conn:
        return changelog.import_snapshot(conn, snapshot)


async def _sync_snapshot(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"{state.leader_url}/ha/snapshot", headers=_headers(), timeout=60.0)
    resp.raise_for_status()
    snapshot = resp.json()
    state.applied_seq = await run_in_threadpool(_import, snapshot)
    state.need_snapshot = False
    state.last_leader_contact = time.monotonic()
    logger.info("Snapshot del líder aplicado (seq %d)", state.applied_seq)


async def _follow_once(client: httpx.AsyncClient) -> None:
    if state.need_snapshot:
        await _sync_snapshot(client)
    resp = await client.get(
        f"{state.leader_url}/ha/log",
        params={"since": state.applied_seq, "wait": settings.ha_poll_wait},
        headers=_headers(),
        timeout=settings.ha_poll_wait + 5.0,
    )
    if resp.status_code == 410:
        state.need_snapshot = True
        return
    if resp.status_code == 409:
        # Ya no es líder: buscar otro.
        state.leader_url = None
        return
    resp.raise_for_status()
    body = resp.json()
    state.last_leader_contact = time.monotonic()
    state.leader_id = body.get("leader_id")
    if int(body.get("epoch", 0)) > state.epoch:
        state.epoch = int(body["epoch"])
        save_epoch(state.epoch)
    if body["entries"]:
        state.applied_seq = await run_in_threadpool(_apply, body["entries"])


async def _find_leader(client: httpx.AsyncClient) -> bool:
    peers = await _peer_statuses(client)
    leaders = [(url, st) for url, st in peers if st.get("role") == LEADER]
    if not leaders:
        return False
    url, st = max(leaders, key=lambda item: int(item[1].get("epoch", 0)))
    _become_standby(url, st.get("controlnode_id"), int(st.get("epoch", 0)), snapshot=True)
    return True


async def _maybe_promote(client: httpx.AsyncClient) -> None:
    silent_for = time.monotonic() - state.last_leader_contact
    if silent_for < settings.ha_failover_seconds:
        return
    peers = await _peer_statuses(client)
    if any(st.get("role") == LEADER for _, st in peers):
        await _find_leader(client)
        return
    if state.leader_id:
        reporters, seeing = state.witnesses_seeing(state.leader_id, settings.ha_failover_seconds)
        if reporters and seeing * 2 >= reporters:
            logger.warning(
                "No alcanzo al líder %s pero %d/%d DataNodes sí: no me promuevo (posible partición)",
                state.leader_id,
                seeing,
                reporters,
            )
            return
    mine = (state.applied_seq, _neg_id(settings.controlnode_id))
    for _, st in peers:
        if st.get("role") == STANDBY and (int(st.get("applied_seq", 0)), _neg_id(st.get("controlnode_id", ""))) > mine:
            return  # otro respaldo está más al día: que se promueva él
    known_epoch = max([state.epoch] + [int(st.get("epoch", 0)) for _, st in peers])
    _become_leader(known_epoch + 1)


def _neg_id(node_id: str) -> tuple:
    # Para desempatar a favor del id menor dentro de una tupla "mayor gana".
    return tuple(-ord(char) for char in node_id)


# --------------------------------------------------------------------------
# Líder
# --------------------------------------------------------------------------
async def _check_peers_as_leader(client: httpx.AsyncClient) -> None:
    for url, st in await _peer_statuses(client):
        if st.get("role") != LEADER:
            continue
        other_epoch = int(st.get("epoch", 0))
        other_id = st.get("controlnode_id", "")
        if other_epoch > state.epoch or (other_epoch == state.epoch and other_id < settings.controlnode_id):
            logger.warning("Hay otro líder %s con época %d: me degrado", other_id, other_epoch)
            _become_standby(url, other_id, other_epoch, snapshot=True)
            return


def _prune() -> None:
    with engine.begin() as conn:
        changelog.prune(conn, settings.ha_log_retention)


async def run(stop: asyncio.Event) -> None:
    """Bucle de HA (solo si hay pares configurados)."""
    if not state.enabled:
        return
    last_prune = time.monotonic()
    async with httpx.AsyncClient(verify=_verify()) as client:
        while not stop.is_set():
            try:
                if state.role == STANDBY:
                    if state.leader_url is None and not await _find_leader(client):
                        await _maybe_promote(client)
                        await _sleep(stop, 1.0)
                        continue
                    try:
                        await _follow_once(client)
                    except httpx.HTTPError as exc:
                        logger.info("Sin contacto con el líder: %s", exc)
                        await _maybe_promote(client)
                        await _sleep(stop, 0.5)
                else:
                    await _check_peers_as_leader(client)
                    if time.monotonic() - last_prune > PRUNE_INTERVAL:
                        await run_in_threadpool(_prune)
                        last_prune = time.monotonic()
                    await _sleep(stop, PEER_CHECK_INTERVAL)
            except Exception:  # noqa: BLE001 - el bucle de HA nunca debe morir
                logger.exception("Error en el bucle de HA")
                await _sleep(stop, 1.0)


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
