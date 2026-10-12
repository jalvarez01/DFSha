import importlib
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

TEST_SECRET = "secreto-de-clúster-de-test"
NODE_ID = "dn-test"


def _sign(payload: dict, secret: str = TEST_SECRET, ttl: timedelta = timedelta(hours=1)) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"iat": now, "exp": now + ttl, **payload}, secret, algorithm="HS256")


def pipe_entry(node_id: str, host: str = "127.0.0.1", port: int = 1) -> dict:
    return {"node_id": node_id, "scheme": "http", "host": host, "port": port}


def write_token(block_id: str, file_id: str = "7", pipe: list | None = None, **overrides) -> str:
    payload = {
        "typ": "block",
        "bid": block_id,
        "fid": file_id,
        "op": "write",
        "sub": "tester",
        "pipe": pipe if pipe is not None else [pipe_entry(NODE_ID)],
    }
    payload.update(overrides)
    return _sign(payload)


def read_token(block_id: str, **overrides) -> str:
    payload = {"typ": "block", "bid": block_id, "fid": "7", "op": "read", "sub": "tester"}
    payload.update(overrides)
    return _sign(payload)


def controlnode_token() -> str:
    return _sign({"typ": "node", "sub": "cn1", "role": "controlnode"})


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def put(client, block_id, data, file_id="7", token=None, **headers):
    return client.put(
        f"/blocks/{block_id}",
        params={"file_id": file_id},
        content=data,
        headers={**auth(token or write_token(block_id, file_id)), **headers},
    )


def get(client, block_id, token=None):
    return client.get(f"/blocks/{block_id}", headers=auth(token or read_token(block_id)))


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    # Aislar cada test en su propio storage_dir y evitar que el lifespan
    # intente de verdad hablar con un ControlNode.
    monkeypatch.setenv("DFSHA_DATANODE_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("DFSHA_CONTROLNODE_URL", "http://controlnode-inexistente.invalid")
    monkeypatch.setenv("DFSHA_HEARTBEAT_INTERVAL", "3600")
    monkeypatch.setenv("DFSHA_CLUSTER_SECRET", TEST_SECRET)
    monkeypatch.setenv("DFSHA_DATANODE_ID", NODE_ID)

    from app import config as config_module

    importlib.reload(config_module)
    for name in ("security", "cluster", "registration", "main"):
        importlib.reload(importlib.import_module(f"app.{name}"))
    from app import main as main_module

    return main_module


@pytest.fixture()
def client(app_module):
    with TestClient(app_module.app) as test_client:
        yield test_client
