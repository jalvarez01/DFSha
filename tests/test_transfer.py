"""
Tests del router de transferencia (RF2): PUT y GET de archivos.

Se monta un mini-app de FastAPI que solo incluye el router de
transferencia (no el `app` completo de app.main), para no depender del
lifespan de main.py (que siembra la raíz contra la BD "real" configurada
en app.database, no contra la sesión SQLite en memoria de la fixture
`db`). Esto mantiene los tests rápidos, aislados y sin tocar disco salvo
por el storage temporal de blobs, que se redirige a tmp_path.
"""
import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.models import Directory
from app.path_service import get_root_directory
from app.routers import transfer as transfer_module


@pytest.fixture()
def client(db, tmp_path, monkeypatch):
    # Los blobs de este test van a un directorio temporal, no al
    # STORAGE_DIR real del proceso.
    monkeypatch.setattr(transfer_module, "STORAGE_DIR", tmp_path)

    app = FastAPI()
    app.include_router(transfer_module.router, prefix="/files")
    app.dependency_overrides[get_db] = lambda: db

    with TestClient(app) as test_client:
        test_client.headers.update({"X-Username": "tester"})
        yield test_client


def test_put_creates_file_and_returns_metadata(client):
    resp = client.put("/files/report.txt", content=b"hola mundo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True
    assert body["file"]["path"] == "/report.txt"
    assert body["file"]["size_bytes"] == len(b"hola mundo")
    assert body["file"]["owner"] == "tester"


def test_put_then_get_roundtrip(client):
    content = b"contenido de prueba " * 1000  # fuerza varios chunks internos
    client.put("/files/data.bin", content=content)

    resp = client.get("/files/data.bin")
    assert resp.status_code == 200
    assert resp.content == content
    assert resp.headers["X-Checksum-Sha256"] == hashlib.sha256(content).hexdigest()


def test_put_overwrite_updates_existing_file(client):
    client.put("/files/data.bin", content=b"version 1")
    resp = client.put("/files/data.bin", content=b"version 2, mas larga")
    assert resp.status_code == 200
    assert resp.json()["created"] is False

    resp = client.get("/files/data.bin")
    assert resp.content == b"version 2, mas larga"


def test_get_nonexistent_file_returns_404(client):
    resp = client.get("/files/no-existe.txt")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


def test_put_fails_when_name_is_a_directory(client, db, owner_id):
    root = get_root_directory(db)
    d = Directory(name="carpeta", parent_id=root.id, owner_id=owner_id)
    db.add(d)
    db.commit()

    resp = client.put("/files/carpeta", content=b"algo")
    assert resp.status_code == 409


def test_head_returns_size_and_checksum_without_body(client):
    content = b"abc123"
    client.put("/files/small.txt", content=content)

    resp = client.head("/files/small.txt")
    assert resp.status_code == 200
    assert resp.headers["Content-Length"] == str(len(content))
    assert resp.headers["X-Checksum-Sha256"] == hashlib.sha256(content).hexdigest()
    assert resp.content == b""
