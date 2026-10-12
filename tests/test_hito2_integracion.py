"""
Tests de integración del Hito 2 en el ControlNode: la convivencia entre el
PUT/GET monolítico del Hito 1 (app/routers/transfer.py) y los archivos
distribuidos en bloques, y la migración de la tabla `blocks`.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app import placement
from app.database import get_db
from app.main import _migrate_blocks_table
from app.models import BLOCKS_UNIQUE_CONSTRAINT
from app.routers import control as control_module
from app.routers import datanodes as datanodes_module
from app.routers import fs as fs_module
from app.routers import transfer as transfer_module
from tests.helpers import deletions_for, register, write_file


@pytest.fixture()
def client(db, tmp_path, monkeypatch):
    placement.reset_cursor()
    monkeypatch.setattr(transfer_module, "STORAGE_DIR", tmp_path)

    app = FastAPI()
    app.include_router(datanodes_module.router, prefix="/datanodes")
    app.include_router(fs_module.router, prefix="/fs")
    # Mismo orden que app/main.py: control antes que el catch-all.
    app.include_router(control_module.router, prefix="/files")
    app.include_router(transfer_module.router, prefix="/files")
    app.dependency_overrides[get_db] = lambda: db

    with TestClient(app) as test_client:
        test_client.headers.update({"X-Username": "tester"})
        client_register(test_client)
        yield test_client


def client_register(client):
    register(client, "dn-0")


def _distributed_write(client, path, size):
    return write_file(client, path, size)


def _pending(client):
    return deletions_for(client, "dn-0")


def test_get_heredado_de_archivo_distribuido_es_409_y_no_500(client):
    _distributed_write(client, "/dist.bin", 15)
    resp = client.get("/files/dist.bin")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "distributed_file"


def test_head_de_archivo_distribuido_da_su_tamano(client):
    _distributed_write(client, "/dist.bin", 15)
    resp = client.head("/files/dist.bin")
    assert resp.status_code == 200
    assert resp.headers["content-length"] == "15"


def test_put_heredado_sobre_archivo_distribuido_descarta_sus_bloques(client):
    plan = _distributed_write(client, "/dist.bin", 15)
    resp = client.put("/files/dist.bin", content=b"ahora soy monolitico")
    assert resp.status_code == 200

    assert client.get("/files/dist.bin").content == b"ahora soy monolitico"
    assert client.get("/files/dist.bin/blocks").json()["num_blocks"] == 0
    assert _pending(client) == {b["block_id"] for b in plan["blocks"]}


def test_allocate_sobre_archivo_heredado_borra_su_blob_local(client, tmp_path):
    put = client.put("/files/viejo.txt", content=b"hito 1").json()
    file_id = put["file"]["id"]
    assert (tmp_path / str(file_id)).exists()

    _distributed_write(client, "/viejo.txt", 12)

    assert not (tmp_path / str(file_id)).exists()
    assert not (tmp_path / f"{file_id}.sha256").exists()
    assert client.get("/files/viejo.txt").json()["detail"]["error"] == "distributed_file"


def test_rm_de_archivo_heredado_no_encola_borrados(client, tmp_path):
    client.put("/files/viejo.txt", content=b"hito 1")
    assert client.request("DELETE", "/fs/rm", json={"path": "/viejo.txt"}).status_code == 200
    assert _pending(client) == set()
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# Migración de la tabla blocks (BD creadas con la unicidad vieja)
# --------------------------------------------------------------------------
_OLD_BLOCKS_DDL = """
CREATE TABLE blocks (
    id INTEGER NOT NULL,
    block_id VARCHAR(64) NOT NULL,
    file_id INTEGER NOT NULL,
    block_index INTEGER NOT NULL,
    size_bytes BIGINT NOT NULL,
    checksum VARCHAR(64),
    datanode_id INTEGER NOT NULL,
    committed BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_block_file_index UNIQUE (file_id, block_index)
)
"""


def test_migracion_de_blocks_conserva_filas_y_cambia_la_unicidad(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'vieja.db'}")
    with engine.begin() as conn:
        conn.execute(text(_OLD_BLOCKS_DDL))
        conn.execute(text("CREATE UNIQUE INDEX ix_blocks_block_id ON blocks (block_id)"))
        conn.execute(
            text(
                "INSERT INTO blocks VALUES (1, 'b1', 7, 0, 10, 'abc', 1, 1, '2026-09-01 00:00:00')"
            )
        )

    _migrate_blocks_table(engine)

    names = {uc["name"] for uc in inspect(engine).get_unique_constraints("blocks")}
    assert names == {BLOCKS_UNIQUE_CONSTRAINT}
    with engine.begin() as conn:
        assert conn.execute(text("SELECT block_id, checksum FROM blocks")).all() == [("b1", "abc")]
        # Ahora caben una fila confirmada y una pendiente en el mismo índice.
        conn.execute(
            text(
                "INSERT INTO blocks VALUES (2, 'b2', 7, 0, 3, NULL, 1, 0, '2026-09-01 00:00:00')"
            )
        )

    # Idempotente: una segunda pasada no hace nada.
    _migrate_blocks_table(engine)
    engine.dispose()
