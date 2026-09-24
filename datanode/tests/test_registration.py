"""Tests del ciclo register/heartbeat contra un ControlNode falso,
incluidas las órdenes de borrado (RF1 distribuido)."""
import asyncio

import httpx
import pytest

from app import registration


class FakeControlNode:
    """Simula /datanodes/register y /heartbeat. Entrega `orders` en el
    primer heartbeat y se detiene tras `stop_after` heartbeats."""

    def __init__(self, orders, stop_event, stop_after=3, forget_node_once=False):
        self.orders = list(orders)
        self.stop_event = stop_event
        self.stop_after = stop_after
        self.forget_node_once = forget_node_once
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        self.calls.append((request.url.path, body))
        if request.url.path == "/datanodes/register":
            return httpx.Response(200, json={})
        heartbeats = [c for c in self.calls if c[0] == "/datanodes/heartbeat"]
        if self.forget_node_once and len(heartbeats) == 1:
            return httpx.Response(404, json={"detail": {"error": "unknown_datanode"}})
        if len(heartbeats) >= self.stop_after:
            self.stop_event.set()
        pending = [b for b in self.orders if b not in body["deleted_blocks"]]
        self.orders = pending
        return httpx.Response(200, json={"delete_blocks": pending})


def _run(fake, on_delete, monkeypatch):
    monkeypatch.setattr(registration.settings, "heartbeat_interval", 0.001)

    async def main():
        await asyncio.wait_for(
            registration.register_loop(
                fake.stop_event, on_delete=on_delete, transport=httpx.MockTransport(fake.handler)
            ),
            timeout=5,
        )

    asyncio.run(main())


def test_borra_las_ordenes_y_las_acusa(monkeypatch):
    stop = asyncio.Event()
    fake = FakeControlNode(["b1", "b2"], stop)
    deleted = []
    _run(fake, deleted.append, monkeypatch)

    assert deleted == ["b1", "b2"]
    paths = [c[0] for c in fake.calls]
    assert paths[0] == "/datanodes/register"
    heartbeats = [c[1] for c in fake.calls if c[0] == "/datanodes/heartbeat"]
    assert heartbeats[0]["deleted_blocks"] == []
    assert heartbeats[1]["deleted_blocks"] == ["b1", "b2"]
    # Una vez acusadas, no se vuelven a acusar.
    assert heartbeats[2]["deleted_blocks"] == []


def test_si_falla_el_borrado_no_acusa_y_reintenta(monkeypatch):
    stop = asyncio.Event()
    fake = FakeControlNode(["b1"], stop, stop_after=3)
    attempts = []

    def flaky(block_id):
        attempts.append(block_id)
        if len(attempts) == 1:
            raise OSError("disco ocupado")

    _run(fake, flaky, monkeypatch)
    heartbeats = [c[1] for c in fake.calls if c[0] == "/datanodes/heartbeat"]
    assert attempts == ["b1", "b1"]
    assert heartbeats[1]["deleted_blocks"] == []
    assert heartbeats[2]["deleted_blocks"] == ["b1"]


def test_se_re_registra_si_el_controlnode_no_lo_conoce(monkeypatch):
    stop = asyncio.Event()
    fake = FakeControlNode([], stop, stop_after=2, forget_node_once=True)
    _run(fake, lambda b: None, monkeypatch)
    paths = [c[0] for c in fake.calls]
    assert paths[:3] == ["/datanodes/register", "/datanodes/heartbeat", "/datanodes/register"]


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    monkeypatch.setattr(registration.settings, "controlnode_url", "http://cn.test")
