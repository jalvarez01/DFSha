import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Aislar cada test en su propio storage_dir y evitar que el lifespan
    # intente de verdad hablar con un ControlNode.
    monkeypatch.setenv("DFSHA_DATANODE_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("DFSHA_CONTROLNODE_URL", "http://controlnode-inexistente.invalid")
    monkeypatch.setenv("DFSHA_HEARTBEAT_INTERVAL", "3600")

    import importlib

    from app import config as config_module

    importlib.reload(config_module)

    from app import main as main_module

    importlib.reload(main_module)

    with TestClient(main_module.app) as test_client:
        yield test_client


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_put_then_get_roundtrip(client):
    put_resp = client.put("/blocks/blk-1", params={"file_id": "7"}, content=b"hola bloque")
    assert put_resp.status_code == 200
    body = put_resp.json()
    assert body["size_bytes"] == len(b"hola bloque")

    get_resp = client.get("/blocks/blk-1")
    assert get_resp.status_code == 200
    assert get_resp.content == b"hola bloque"


def test_get_missing_block_is_404(client):
    resp = client.get("/blocks/no-existe")
    assert resp.status_code == 404


def test_delete_block(client):
    client.put("/blocks/blk-1", params={"file_id": "7"}, content=b"data")
    del_resp = client.delete("/blocks/blk-1")
    assert del_resp.status_code == 200
    assert client.get("/blocks/blk-1").status_code == 404


def test_delete_missing_block_is_404(client):
    assert client.delete("/blocks/no-existe").status_code == 404


def test_delete_file_blocks(client):
    client.put("/blocks/blk-1", params={"file_id": "7"}, content=b"a")
    client.put("/blocks/blk-2", params={"file_id": "7"}, content=b"b")
    resp = client.delete("/files/7/blocks")
    assert resp.status_code == 200
    assert resp.json()["blocks_deleted"] == 2
    assert client.get("/blocks/blk-1").status_code == 404
    assert client.get("/blocks/blk-2").status_code == 404
