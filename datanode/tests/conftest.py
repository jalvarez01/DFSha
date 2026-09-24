import importlib
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

TEST_SECRET = "secreto-de-test"


def make_token(secret: str = TEST_SECRET, **overrides) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": "tester", "uid": 1, "iat": now, "exp": now + timedelta(hours=1)}
    payload.update(overrides)
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    # Aislar cada test en su propio storage_dir y evitar que el lifespan
    # intente de verdad hablar con un ControlNode.
    monkeypatch.setenv("DFSHA_DATANODE_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("DFSHA_CONTROLNODE_URL", "http://controlnode-inexistente.invalid")
    monkeypatch.setenv("DFSHA_HEARTBEAT_INTERVAL", "3600")
    monkeypatch.setenv("DFSHA_JWT_SECRET", TEST_SECRET)

    from app import config as config_module

    importlib.reload(config_module)
    from app import security as security_module

    importlib.reload(security_module)
    from app import registration as registration_module

    importlib.reload(registration_module)
    from app import main as main_module

    importlib.reload(main_module)
    return main_module


@pytest.fixture()
def anon_client(app_module):
    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture()
def client(anon_client):
    anon_client.headers.update({"Authorization": f"Bearer {make_token()}"})
    return anon_client
