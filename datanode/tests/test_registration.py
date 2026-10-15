"""Tests del ciclo register/heartbeat/inventario contra ControlNodes falsos:
órdenes de borrado y de copia, líder y respaldo, failover y fencing por
época."""
import asyncio
import json

import httpx
import pytest

from app import registration


def state():
    # Siempre el objeto actual: otros tests recargan app.cluster.
    return registration.cluster


class FakeControlNode:
    """Un ControlNode falso. `role`/`epoch` se pueden cambiar en caliente."""

    def __init__(self, cn_id, role="leader", epoch=1, deletions=(), replicate=()):
        self.cn_id = cn_id
        self.role = role
        self.epoch = epoch
        self.up = True
        self.known = False
        self.deletions = list(deletions)
        self.replicate = list(replicate)
        self.calls: list[tuple[str, dict]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("caído", request=request)
        body = json.loads(request.content)
        path = request.url.path
        self.calls.append((path, body))
        if path == "/datanodes/register":
            self.known = True
            return httpx.Response(200, json={})
        if path == "/datanodes/block-report":
            return httpx.Response(200, json={"known": 0, "added": 0, "lost": 0, "orphans": 0})
        if path == "/datanodes/heartbeat":
            if self.role != "leader":
                return httpx.Response(200, json={"role": self.role, "epoch": self.epoch, "controlnode_id": self.cn_id})
            if not self.known:
                return httpx.Response(404, json={"detail": {"error": "unknown_datanode"}})
            self.deletions = [b for b in self.deletions if b not in body["deleted_blocks"]]
            replicate, self.replicate = self.replicate, []
            return httpx.Response(
                200,
                json={
                    "role": "leader",
                    "epoch": self.epoch,
                    "controlnode_id": self.cn_id,
                    "delete_blocks": self.deletions,
                    "replicate": replicate,
                },
            )
        return httpx.Response(404)

    def heartbeats(self):
        return [body for path, body in self.calls if path == "/datanodes/heartbeat"]


class Cluster:
    def __init__(self, *nodes):
        self.nodes = {f"http://{n.cn_id}.test": n for n in nodes}

    def handler(self, request):
        return self.nodes[f"{request.url.scheme}://{request.url.host}"].handle(request)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(registration.settings, "heartbeat_interval", 0.001)
    state().leader_url = None
    state().leader_id = None
    state().max_epoch = 0
    state().last_ok.clear()
    state().pending_received.clear()
    state().pending_corrupt.clear()
    state().deleted.clear()


def run(fake_cluster, monkeypatch, until, on_delete=None, on_replicate=None, inventory=list, max_seconds=5):
    monkeypatch.setattr(registration.settings, "controlnode_urls", ",".join(fake_cluster.nodes))
    stop = asyncio.Event()

    async def watchdog():
        for _ in range(int(max_seconds / 0.005)):
            if until():
                break
            await asyncio.sleep(0.005)
        stop.set()

    async def main():
        await asyncio.gather(
            registration.register_loop(
                stop,
                on_delete=on_delete or (lambda b: None),
                on_replicate=on_replicate or (lambda c: None),
                inventory=inventory,
                transport=httpx.MockTransport(fake_cluster.handler),
            ),
            watchdog(),
        )

    asyncio.run(main())


def test_se_registra_manda_inventario_borra_y_acusa(monkeypatch):
    cn = FakeControlNode("cn1", deletions=["b1", "b2"])
    deleted = []
    run(
        Cluster(cn),
        monkeypatch,
        until=lambda: len(cn.heartbeats()) >= 4,
        on_delete=deleted.append,
        inventory=lambda: [{"block_id": "b1", "checksum": "0" * 64, "size_bytes": 1}],
    )
    paths = [path for path, _ in cn.calls]
    assert paths[:2] == ["/datanodes/heartbeat", "/datanodes/register"]
    assert "/datanodes/block-report" in paths
    report = next(body for path, body in cn.calls if path == "/datanodes/block-report")
    assert report["blocks"][0]["block_id"] == "b1"
    assert deleted == ["b1", "b2"]
    assert any(hb["deleted_blocks"] == ["b1", "b2"] for hb in cn.heartbeats())


def test_si_falla_el_borrado_no_acusa_y_reintenta(monkeypatch):
    cn = FakeControlNode("cn1", deletions=["b1"])
    attempts = []

    def flaky(block_id):
        attempts.append(block_id)
        if len(attempts) == 1:
            raise OSError("disco ocupado")

    run(Cluster(cn), monkeypatch, until=lambda: any(hb["deleted_blocks"] == ["b1"] for hb in cn.heartbeats()), on_delete=flaky)
    assert attempts == ["b1", "b1"]


def test_ordenes_de_copia_se_entregan_al_ejecutor(monkeypatch):
    command = {"block_id": "b9", "file_id": 3, "token": "t", "targets": [{"node_id": "dn-x", "host": "h", "port": 1}]}
    cn = FakeControlNode("cn1", replicate=[command])
    received = []
    run(Cluster(cn), monkeypatch, until=lambda: bool(received), on_replicate=received.append)
    assert received == [command]


def test_informes_atrasados_salen_en_el_heartbeat_y_se_limpian(monkeypatch):
    state().queue_received([{"block_id": "b1", "checksum": "0" * 64, "size_bytes": 1}])
    cn = FakeControlNode("cn1")
    cn.known = True
    run(Cluster(cn), monkeypatch, until=lambda: not state().pending_received and cn.heartbeats())
    assert cn.heartbeats()[0]["received_blocks"][0]["block_id"] == "b1"


def test_late_a_todos_y_sigue_al_lider(monkeypatch):
    leader = FakeControlNode("cn1", role="leader", epoch=1)
    standby = FakeControlNode("cn2", role="standby", epoch=1)
    run(Cluster(leader, standby), monkeypatch, until=lambda: len(standby.heartbeats()) >= 3)
    assert state().leader_id == "cn1"
    assert not standby.known  # al respaldo no se le registra
    # El informe de testigo cuenta los ControlNodes con los que hubo contacto.
    witness = standby.heartbeats()[-1]["controlnodes"]
    assert set(witness) == {"cn1", "cn2"}


def test_tras_un_failover_sigue_al_nuevo_lider(monkeypatch):
    old = FakeControlNode("cn1", role="leader", epoch=1)
    new = FakeControlNode("cn2", role="standby", epoch=1, deletions=["b1"])
    deleted = []
    progress = {"switched": False}

    def until():
        if not progress["switched"] and state().leader_id == "cn1":
            old.up = False
            new.role, new.epoch, new.known = "leader", 2, True
            progress["switched"] = True
        return bool(deleted)

    run(Cluster(old, new), monkeypatch, until=until, on_delete=deleted.append)
    assert state().leader_id == "cn2"
    assert state().max_epoch == 2
    assert deleted == ["b1"]


def test_ignora_ordenes_de_un_lider_con_epoca_vieja(monkeypatch):
    stale = FakeControlNode("cn1", role="leader", epoch=1, deletions=["b-importante"])
    stale.known = True
    state().max_epoch = 5  # ya vio un líder de época 5
    deleted = []
    run(Cluster(stale), monkeypatch, until=lambda: len(stale.heartbeats()) >= 3, on_delete=deleted.append)
    assert deleted == []


def test_se_re_registra_si_el_lider_no_lo_conoce(monkeypatch):
    cn = FakeControlNode("cn1")
    cn.known = True
    seen = {"forgot": False}

    def until():
        if len(cn.heartbeats()) >= 1 and not seen["forgot"]:
            cn.known = False  # el ControlNode perdió su estado
            seen["forgot"] = True
        return seen["forgot"] and any(path == "/datanodes/register" for path, _ in cn.calls)

    run(Cluster(cn), monkeypatch, until=until)
    assert any(path == "/datanodes/register" for path, _ in cn.calls)


def test_al_arrancar_se_registra_aunque_el_lider_ya_lo_conozca(monkeypatch):
    """Si cambió la dirección anunciada (otra IP), el registro la actualiza."""
    cn = FakeControlNode("cn1")
    cn.known = True
    run(Cluster(cn), monkeypatch, until=lambda: len(cn.heartbeats()) >= 6)
    registers = [body for path, body in cn.calls if path == "/datanodes/register"]
    # Una sola vez por líder, no en cada heartbeat.
    assert len(registers) == 1
    assert registers[0]["host"] == registration.settings.datanode_advertise_host
