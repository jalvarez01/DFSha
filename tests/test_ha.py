"""
Alta disponibilidad del ControlNode (Hito 3): log de cambios y su
aplicación, snapshot, API /ha, middleware (respaldo que rechaza y líder que
espera la confirmación del respaldo) y las decisiones de failover.
"""
import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cluster_auth import node_auth_headers
from app.config import settings
from app.database import Base
from app.ha import changelog, manager
from app.ha import middleware as ha_middleware
from app.ha.state import LEADER, STANDBY, state
from app.models import Directory, File, User
from app.path_service import get_or_create_root
from tests.conftest import as_user
from tests.helpers import register_n, write_file


@pytest.fixture(autouse=True)
def _hooks():
    changelog.install_hooks()


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _dump(engine):
    with engine.connect() as conn:
        return {t.name: sorted(map(tuple, conn.execute(select(t)).all())) for t in changelog.replicated_tables()}


def _leader_mode():
    state.enabled = True
    state.role = LEADER
    state.epoch = 1


# --------------------------------------------------------------------------
# Log de cambios
# --------------------------------------------------------------------------
def test_no_se_registra_nada_sin_ha():
    engine = _engine()
    db = sessionmaker(bind=engine)()
    db.add(User(username="ana"))
    db.commit()
    with engine.connect() as conn:
        assert changelog.read_entries(conn, 0) == []


def test_el_respaldo_reconstruye_exactamente_el_estado_del_lider():
    _leader_mode()
    leader, standby = _engine(), _engine()
    db = sessionmaker(bind=leader)()
    user = User(username="ana")
    db.add(user)
    db.commit()
    root = get_or_create_root(db, user.id)
    docs = Directory(name="docs", parent=root, owner_id=user.id)
    db.add(docs)
    db.flush()
    db.add(File(name="a.txt", parent_id=docs.id, owner_id=user.id, size_bytes=1))
    db.commit()
    docs.mode = 0o750
    db.commit()
    db.delete(docs)  # cascada: borra también a.txt
    db.add(Directory(name="otro", parent=root, owner_id=user.id))
    db.commit()

    with leader.connect() as conn:
        entries = changelog.read_entries(conn, 0)
    assert {e["op"] for e in entries} == {"upsert", "delete"}
    with standby.begin() as conn:
        assert changelog.apply_entries(conn, entries) == entries[-1]["seq"]
    assert _dump(leader) == _dump(standby)
    # El respaldo guarda el log con la misma numeración.
    assert changelog.local_last_seq(standby) == entries[-1]["seq"]


def test_una_transaccion_deshecha_no_deja_entradas():
    _leader_mode()
    engine = _engine()
    db = sessionmaker(bind=engine)()
    db.add(User(username="ana"))
    db.flush()
    db.rollback()
    with engine.connect() as conn:
        assert changelog.read_entries(conn, 0) == []


def test_snapshot_reemplaza_todo_y_continua_la_numeracion():
    _leader_mode()
    leader, standby = _engine(), _engine()
    db = sessionmaker(bind=leader)()
    db.add_all([User(username="ana"), User(username="bob")])
    db.commit()
    # El respaldo tiene basura propia (p. ej. un líder viejo que vuelve).
    sdb = sessionmaker(bind=standby)()
    state.enabled = False
    sdb.add(User(username="divergente"))
    sdb.commit()
    _leader_mode()

    with leader.connect() as conn:
        snapshot = changelog.export_snapshot(conn)
    with standby.begin() as conn:
        seq = changelog.import_snapshot(conn, snapshot)
    assert _dump(leader) == _dump(standby)
    assert seq == changelog.local_last_seq(standby) == snapshot["seq"]


def test_poda_del_log_conserva_la_ultima_entrada():
    _leader_mode()
    engine = _engine()
    db = sessionmaker(bind=engine)()
    for i in range(10):
        db.add(User(username=f"u{i}"))
        db.commit()
    with engine.begin() as conn:
        changelog.prune(conn, retention=3)
    with engine.connect() as conn:
        assert changelog.first_seq(conn) == changelog.last_seq(conn) - 2


# --------------------------------------------------------------------------
# API /ha y replicación a través de la API real
# --------------------------------------------------------------------------
@pytest.fixture()
def ha_client(db, monkeypatch):
    from app.database import get_db
    from app.ha import router as ha_router_module

    app = FastAPI()
    app.include_router(ha_router_module.router, prefix="/ha")
    app.dependency_overrides[get_db] = lambda: db
    # /ha/log lee con el engine global: se apunta al de la fixture.
    monkeypatch.setattr(ha_router_module, "engine", db.get_bind())
    with TestClient(app) as client:
        yield client


def _cn():
    return node_auth_headers("cn2", "controlnode")


def test_status_es_publico(ha_client):
    _leader_mode()
    body = ha_client.get("/ha/status").json()
    assert body["role"] == "leader"
    assert body["controlnode_id"] == settings.controlnode_id


def test_log_exige_token_de_controlnode(ha_client):
    _leader_mode()
    assert ha_client.get("/ha/log", params={"since": 0}).status_code == 401
    assert ha_client.get("/ha/log", params={"since": 0}, headers=node_auth_headers("dn-1", "datanode")).status_code == 401


def test_log_devuelve_entradas_y_registra_el_acuse(ha_client, db):
    _leader_mode()
    db.add(User(username="ana"))
    db.commit()
    body = ha_client.get("/ha/log", params={"since": 0}, headers=_cn()).json()
    assert [e["table"] for e in body["entries"]] == ["users"]
    ha_client.get("/ha/log", params={"since": body["last_seq"]}, headers=_cn())
    assert state.healthy_followers() == {"cn2": body["last_seq"]}


def test_log_pide_snapshot_si_el_respaldo_va_por_delante(ha_client):
    _leader_mode()
    resp = ha_client.get("/ha/log", params={"since": 999}, headers=_cn())
    assert resp.status_code == 410
    assert resp.json()["detail"]["error"] == "snapshot_required"


def test_un_respaldo_no_sirve_el_log(ha_client):
    state.enabled, state.role = True, STANDBY
    assert ha_client.get("/ha/log", params={"since": 0}, headers=_cn()).status_code == 409


def test_escrituras_por_la_api_se_replican_al_respaldo(api, db):
    _leader_mode()
    # Como en el sistema real: el respaldo arranca con un snapshot y luego
    # aplica el log desde ahí.
    standby = _engine()
    with db.get_bind().connect() as conn:
        snapshot = changelog.export_snapshot(conn)
    with standby.begin() as conn:
        changelog.import_snapshot(conn, snapshot)

    register_n(api, 3)
    api.headers.update(as_user("ana"))
    write_file(api, "/a.bin", 25)
    api.post("/fs/mkdir", json={"path": "/docs"})
    api.post("/fs/chmod", json={"path": "/docs", "mode": "750"})
    api.request("DELETE", "/fs/rm", json={"path": "/a.bin"})

    with db.get_bind().connect() as conn:
        entries = changelog.read_entries(conn, snapshot["seq"], limit=100_000)
    with standby.begin() as conn:
        changelog.apply_entries(conn, entries)
    assert _dump(db.get_bind()) == _dump(standby)


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------
def _app_with_middleware():
    app = FastAPI()
    app.add_middleware(ha_middleware.HAMiddleware)

    @app.get("/fs/ls")
    def ls():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/datanodes/heartbeat")
    def hb():
        return {"ok": True}

    return app


def test_un_respaldo_rechaza_peticiones_con_not_leader():
    state.enabled, state.role, state.leader_id = True, STANDBY, "cn1"
    with TestClient(_app_with_middleware()) as client:
        resp = client.get("/fs/ls")
        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "not_leader"
        assert resp.headers["x-dfsha-leader"] == "cn1"
        assert client.get("/health").status_code == 200
        assert client.post("/datanodes/heartbeat").status_code == 200


def test_el_lider_atiende_normalmente():
    _leader_mode()
    with TestClient(_app_with_middleware()) as client:
        assert client.get("/fs/ls").status_code == 200


def test_espera_semi_sincrona():
    _leader_mode()

    async def scenario():
        # Sin respaldos sanos: no espera.
        start = time.monotonic()
        assert await ha_middleware.wait_for_followers(5, timeout=1.0)
        assert time.monotonic() - start < 0.1
        # Respaldo atrasado: espera hasta el timeout y sigue (disponibilidad).
        state.record_follower("cn2", 3)
        start = time.monotonic()
        assert not await ha_middleware.wait_for_followers(5, timeout=0.2)
        assert time.monotonic() - start >= 0.2
        # El respaldo confirma mientras se espera: termina enseguida.

        async def ack_later():
            await asyncio.sleep(0.05)
            state.record_follower("cn2", 5)

        start = time.monotonic()
        results = await asyncio.gather(ha_middleware.wait_for_followers(5, timeout=2.0), ack_later())
        assert results[0] is True
        assert time.monotonic() - start < 1.0

    asyncio.run(scenario())


def test_una_escritura_por_la_api_espera_al_respaldo(db, monkeypatch):
    """De punta a punta: la respuesta de una escritura no sale hasta que el
    respaldo confirma su entrada del log."""
    from app.database import get_db
    from app.routers import fs

    _leader_mode()
    calls = []

    async def fake_wait(seq, timeout=None):
        calls.append(seq)
        return True

    monkeypatch.setattr(ha_middleware, "wait_for_followers", fake_wait)
    app = FastAPI()
    app.add_middleware(ha_middleware.HAMiddleware)
    app.include_router(fs.router, prefix="/fs")
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        client.post("/fs/mkdir", json={"path": "/x"}, headers=as_user("ana"))
        assert calls and calls[-1] == changelog.local_last_seq(db.get_bind())
        calls.clear()
        client.get("/fs/ls", params={"path": "/"}, headers=as_user("ana"))
        assert calls == []  # una lectura no espera


# --------------------------------------------------------------------------
# Failover: decisiones del respaldo y degradación del líder viejo
# --------------------------------------------------------------------------
def _standby_waiting(seconds_without_leader: float, leader_id="cn1"):
    state.enabled, state.role, state.leader_id, state.epoch = True, STANDBY, leader_id, 1
    state.last_leader_contact = time.monotonic() - seconds_without_leader


@pytest.fixture()
def peers(monkeypatch):
    statuses: list = []

    async def fake_statuses(client):
        return list(statuses)

    monkeypatch.setattr(manager, "_peer_statuses", fake_statuses)
    monkeypatch.setattr(manager, "save_epoch", lambda epoch: None)
    return statuses


def _promote_attempt():
    asyncio.run(manager._maybe_promote(client=None))


def test_no_se_promueve_antes_del_plazo(peers):
    _standby_waiting(settings.ha_failover_seconds / 2)
    _promote_attempt()
    assert state.role == STANDBY


def test_se_promueve_si_el_lider_calla_y_los_testigos_tampoco_lo_ven(peers):
    _standby_waiting(settings.ha_failover_seconds + 1)
    for dn in ("dn-1", "dn-2", "dn-3"):
        state.record_witness(dn, {"cn1": settings.ha_failover_seconds + 5, "cn2": 1.0})
    _promote_attempt()
    assert state.role == LEADER
    assert state.epoch == 2


def test_no_se_promueve_si_la_mayoria_de_los_datanodes_ve_al_lider(peers):
    """Partición: el respaldo no alcanza al líder, pero el clúster sí."""
    _standby_waiting(settings.ha_failover_seconds + 1)
    state.record_witness("dn-1", {"cn1": 2.0})
    state.record_witness("dn-2", {"cn1": 3.0})
    state.record_witness("dn-3", {"cn1": 999.0})
    _promote_attempt()
    assert state.role == STANDBY


def test_no_se_promueve_si_otro_par_ya_es_lider(peers):
    _standby_waiting(settings.ha_failover_seconds + 1)
    peers.append(("http://cn3", {"role": "leader", "epoch": 2, "controlnode_id": "cn3"}))
    _promote_attempt()
    assert state.role == STANDBY
    assert state.leader_url == "http://cn3"


def test_cede_ante_un_respaldo_mas_al_dia(peers):
    _standby_waiting(settings.ha_failover_seconds + 1)
    state.applied_seq = 10
    peers.append(("http://cn3", {"role": "standby", "epoch": 1, "controlnode_id": "cn3", "applied_seq": 20}))
    _promote_attempt()
    assert state.role == STANDBY


def test_un_lider_que_ve_otro_lider_con_epoca_mayor_se_degrada(peers):
    _leader_mode()
    peers.append(("http://cn2", {"role": "leader", "epoch": 2, "controlnode_id": "cn2"}))
    asyncio.run(manager._check_peers_as_leader(client=None))
    assert state.role == STANDBY
    assert state.leader_id == "cn2"
    assert state.need_snapshot is True  # descarta lo que no llegó a replicar


def test_heartbeat_en_un_respaldo_no_toca_la_bd_y_anota_el_testigo(api, db):
    from app.models import DataNode
    from tests.helpers import heartbeat

    register_n(api, 1)
    state.enabled, state.role, state.epoch = True, STANDBY, 3
    before = db.query(DataNode).one().last_heartbeat
    resp = heartbeat(api, "dn-0", controlnodes={"cn1": 4.0}).json()
    assert resp["role"] == "standby"
    assert resp["epoch"] == 3
    assert resp["delete_blocks"] == [] and resp["replicate"] == []
    db.expire_all()
    assert db.query(DataNode).one().last_heartbeat == before
    assert state.witnesses_seeing("cn1", within=10.0) == (1, 1)
