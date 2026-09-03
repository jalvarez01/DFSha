"""
Tests del router de gestión del filesystem (RF1): ls / mkdir / rmdir / rm / stat.

Se monta un mini-app con AMBOS routers (fs y transfer, este último con su
prefijo real "/files"), no el `app` de app.main: así los tests no dependen del
lifespan, que sembraría la raíz contra la BD real en vez de contra la SQLite
en memoria de la fixture `db`. Montar los dos permite además probar lo que
ningún router puede probar por separado: que al borrar metadatos con RF1
también desaparezca del disco el contenido que subió RF2.

Dos detalles del entorno que condicionan cómo se escriben estos tests:

- httpx 0.27 (el pinneado en requirements.txt) no acepta `json=` en
  TestClient.delete(): hay que usar client.request("DELETE", ...). Con
  `json=` sería un TypeError, no un 422.
- Los objetos que el endpoint borra quedan desacoplados de la sesión, así que
  los borrados se comprueban re-consultando (db.get(...) is None), nunca
  leyendo atributos del objeto borrado.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.models import Directory, File
from app.path_service import get_root_directory
from app.routers import fs as fs_module
from app.routers import transfer as transfer_module


@pytest.fixture()
def client(db, tmp_path, monkeypatch):
    # Los blobs de estos tests van a un directorio temporal. fs.py usa las
    # funciones _blob_path/_checksum_path de transfer, que leen este global en
    # tiempo de llamada, así que el parche también aplica al borrado.
    monkeypatch.setattr(transfer_module, "STORAGE_DIR", tmp_path)

    app = FastAPI()
    app.include_router(fs_module.router, prefix="/fs")
    app.include_router(transfer_module.router, prefix="/files")
    app.dependency_overrides[get_db] = lambda: db

    with TestClient(app) as test_client:
        test_client.headers.update({"X-Username": "tester"})
        yield test_client


def _mkdir(client, path: str, parents: bool = False):
    return client.post("/fs/mkdir", json={"path": path, "parents": parents})


def _rmdir(client, path: str, recursive: bool = False):
    return client.request(
        "DELETE", "/fs/rmdir", json={"path": path, "recursive": recursive}
    )


def _rm(client, path: str):
    return client.request("DELETE", "/fs/rm", json={"path": path})


def _names(body) -> list[str]:
    return [entry["name"] for entry in body["entries"]]


# --------------------------------------------------------------------------
# ls
# --------------------------------------------------------------------------
def test_ls_empty_root(client):
    resp = client.get("/fs/ls", params={"path": "/"})
    assert resp.status_code == 200
    assert resp.json() == {"path": "/", "entries": []}


def test_ls_lists_directories_before_files(client):
    _mkdir(client, "/zeta")
    _mkdir(client, "/alfa")
    client.put("/files/archivo.txt", content=b"hola")

    body = client.get("/fs/ls", params={"path": "/"}).json()
    assert _names(body) == ["alfa", "zeta", "archivo.txt"]

    tipos = {entry["name"]: entry["type"] for entry in body["entries"]}
    assert tipos == {"alfa": "directory", "zeta": "directory", "archivo.txt": "file"}

    tamanos = {entry["name"]: entry["size_bytes"] for entry in body["entries"]}
    assert tamanos["alfa"] == 0
    assert tamanos["archivo.txt"] == len(b"hola")


def test_ls_nested_directory_returns_normalized_path(client):
    _mkdir(client, "/docs")
    _mkdir(client, "/docs/informes")

    body = client.get("/fs/ls", params={"path": "/docs/./informes/"}).json()
    assert body["path"] == "/docs/informes"
    assert body["entries"] == []


def test_ls_nonexistent_directory_returns_404(client):
    resp = client.get("/fs/ls", params={"path": "/no-existe"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


def test_ls_on_a_file_returns_409(client):
    client.put("/files/archivo.txt", content=b"hola")

    resp = client.get("/fs/ls", params={"path": "/archivo.txt"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_a_directory"


def test_ls_with_unknown_cwd_returns_404(client):
    resp = client.get("/fs/ls", params={"path": "docs", "cwd_id": 9999})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


# --------------------------------------------------------------------------
# mkdir
# --------------------------------------------------------------------------
def test_mkdir_creates_directory(client):
    resp = _mkdir(client, "/docs")
    assert resp.status_code == 201

    body = resp.json()
    assert body["path"] == "/docs"
    assert body["name"] == "docs"
    assert body["id"] > 0

    assert _names(client.get("/fs/ls", params={"path": "/"}).json()) == ["docs"]


def test_mkdir_nested_reports_full_path(client):
    _mkdir(client, "/docs")
    resp = _mkdir(client, "/docs/informes")
    assert resp.status_code == 201
    assert resp.json()["path"] == "/docs/informes"


def test_mkdir_without_existing_parent_returns_404(client):
    resp = _mkdir(client, "/docs/informes")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


def test_mkdir_duplicate_returns_409(client):
    _mkdir(client, "/docs")
    resp = _mkdir(client, "/docs")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "already_exists"


def test_mkdir_over_existing_file_returns_409(client):
    client.put("/files/archivo.txt", content=b"hola")

    resp = _mkdir(client, "/archivo.txt")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "already_exists"


def test_mkdir_root_returns_400(client):
    resp = _mkdir(client, "/")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "root_operation"


def test_mkdir_invalid_name_returns_400(client):
    resp = _mkdir(client, "/docs/mal\x01nombre")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_path"


def test_mkdir_parents_creates_whole_chain(client):
    resp = _mkdir(client, "/a/b/c", parents=True)
    assert resp.status_code == 201
    assert resp.json()["path"] == "/a/b/c"

    assert _names(client.get("/fs/ls", params={"path": "/"}).json()) == ["a"]
    assert _names(client.get("/fs/ls", params={"path": "/a"}).json()) == ["b"]
    assert _names(client.get("/fs/ls", params={"path": "/a/b"}).json()) == ["c"]


def test_mkdir_parents_does_not_create_a_second_root(client, db):
    """Si la cadena se creara sin flush intermedio, los hijos quedarían con
    parent_id=None (la columna es nullable) y aparecerían raíces fantasma."""
    _mkdir(client, "/a/b/c", parents=True)

    roots = db.query(Directory).filter(Directory.parent_id.is_(None)).all()
    assert len(roots) == 1


def test_mkdir_parents_is_idempotent(client):
    _mkdir(client, "/a/b", parents=True)

    resp = _mkdir(client, "/a/b", parents=True)
    assert resp.status_code == 200
    assert resp.json()["path"] == "/a/b"


def test_mkdir_parents_over_existing_file_returns_409(client):
    client.put("/files/archivo.txt", content=b"hola")

    resp = _mkdir(client, "/archivo.txt", parents=True)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "already_exists"


def test_mkdir_parents_through_a_file_returns_409(client):
    client.put("/files/archivo.txt", content=b"hola")

    resp = _mkdir(client, "/archivo.txt/sub", parents=True)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_a_directory"


def test_mkdir_parents_root_returns_400(client):
    resp = _mkdir(client, "/", parents=True)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "root_operation"


# --------------------------------------------------------------------------
# rmdir
# --------------------------------------------------------------------------
def test_rmdir_removes_empty_directory(client, db):
    directory_id = _mkdir(client, "/docs").json()["id"]

    resp = _rmdir(client, "/docs")
    assert resp.status_code == 200
    assert resp.json() == {"path": "/docs", "deleted": True}
    assert db.get(Directory, directory_id) is None


def test_rmdir_non_empty_without_recursive_returns_409(client, db):
    _mkdir(client, "/docs")
    child_id = _mkdir(client, "/docs/informes").json()["id"]

    resp = _rmdir(client, "/docs")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "directory_not_empty"
    # No debe haber borrado nada.
    assert db.get(Directory, child_id) is not None


def test_rmdir_with_only_files_without_recursive_returns_409(client):
    _mkdir(client, "/docs")
    client.put("/files/docs/archivo.txt", content=b"hola")

    resp = _rmdir(client, "/docs")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "directory_not_empty"


def test_rmdir_recursive_deletes_subtree_and_blobs(client, db, tmp_path):
    _mkdir(client, "/docs/informes", parents=True)
    client.put("/files/docs/raiz.txt", content=b"uno")
    client.put("/files/docs/informes/hoja.txt", content=b"dos")

    file_ids = [row.id for row in db.query(File).all()]
    assert len(file_ids) == 2
    assert all((tmp_path / str(file_id)).exists() for file_id in file_ids)

    resp = _rmdir(client, "/docs", recursive=True)
    assert resp.status_code == 200

    assert _names(client.get("/fs/ls", params={"path": "/"}).json()) == []
    assert db.query(Directory).filter(Directory.name == "informes").first() is None
    assert db.query(File).count() == 0
    for file_id in file_ids:
        assert not (tmp_path / str(file_id)).exists()
        assert not (tmp_path / f"{file_id}.sha256").exists()


def test_rmdir_root_returns_400(client, db):
    resp = _rmdir(client, "/")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "root_operation"
    assert get_root_directory(db) is not None


def test_rmdir_on_a_file_returns_409(client):
    client.put("/files/archivo.txt", content=b"hola")

    resp = _rmdir(client, "/archivo.txt")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_a_directory"


def test_rmdir_nonexistent_returns_404(client):
    resp = _rmdir(client, "/no-existe")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


# --------------------------------------------------------------------------
# rm
# --------------------------------------------------------------------------
def test_rm_deletes_metadata_and_blob(client, db, tmp_path):
    file_id = client.put("/files/archivo.txt", content=b"hola").json()["file"]["id"]
    assert (tmp_path / str(file_id)).exists()

    resp = _rm(client, "/archivo.txt")
    assert resp.status_code == 200
    assert resp.json() == {"path": "/archivo.txt", "deleted": True}

    assert db.get(File, file_id) is None
    assert not (tmp_path / str(file_id)).exists()
    assert not (tmp_path / f"{file_id}.sha256").exists()
    assert client.get("/files/archivo.txt").status_code == 404


def test_rm_reports_full_path_of_nested_file(client):
    _mkdir(client, "/docs")
    client.put("/files/docs/archivo.txt", content=b"hola")

    resp = _rm(client, "/docs/archivo.txt")
    assert resp.status_code == 200
    assert resp.json()["path"] == "/docs/archivo.txt"


def test_rm_on_a_directory_returns_409(client, db):
    directory_id = _mkdir(client, "/docs").json()["id"]

    resp = _rm(client, "/docs")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_a_file"
    assert db.get(Directory, directory_id) is not None


def test_rm_nonexistent_returns_404(client):
    resp = _rm(client, "/no-existe.txt")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


# --------------------------------------------------------------------------
# stat
# --------------------------------------------------------------------------
def test_stat_directory(client):
    _mkdir(client, "/docs")

    body = client.get("/fs/stat", params={"path": "/docs"}).json()
    assert body["path"] == "/docs"
    assert body["name"] == "docs"
    assert body["type"] == "directory"
    assert body["size_bytes"] == 0
    assert body["owner"] == "tester"
    assert body["updated_at"] is None


def test_stat_root(client):
    body = client.get("/fs/stat", params={"path": "/"}).json()
    assert body["path"] == "/"
    assert body["name"] == "/"
    assert body["type"] == "directory"


def test_stat_file(client):
    content = b"contenido de prueba"
    client.put("/files/docs.txt", content=content)

    body = client.get("/fs/stat", params={"path": "/docs.txt"}).json()
    assert body["path"] == "/docs.txt"
    assert body["type"] == "file"
    assert body["size_bytes"] == len(content)
    assert body["owner"] == "tester"
    assert body["updated_at"] is not None


def test_stat_nonexistent_returns_404(client):
    resp = client.get("/fs/stat", params={"path": "/no-existe"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


def test_stat_through_a_file_returns_409(client):
    client.put("/files/archivo.txt", content=b"hola")

    resp = client.get("/fs/stat", params={"path": "/archivo.txt/sub"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_a_directory"


def test_stat_invalid_path_returns_400(client):
    resp = client.get("/fs/stat", params={"path": "/mal\x01nombre"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_path"


# --------------------------------------------------------------------------
# Integración con el router de transferencia
# --------------------------------------------------------------------------
def test_put_into_directory_created_with_mkdir(client):
    _mkdir(client, "/docs/informes", parents=True)

    resp = client.put("/files/docs/informes/reporte.txt", content=b"contenido")
    assert resp.status_code == 200
    assert resp.json()["file"]["path"] == "/docs/informes/reporte.txt"

    assert _names(client.get("/fs/ls", params={"path": "/docs/informes"}).json()) == [
        "reporte.txt"
    ]


def test_fs_router_imports_transfer_storage_helpers():
    """fs.py depende de _blob_path/_checksum_path de transfer.py. Si allí se
    renombran, el import falla y se cae la app entera al arrancar; este test
    lo detecta en la suite en vez de en producción."""
    assert fs_module._blob_path(7) == transfer_module.STORAGE_DIR / "7"
    assert fs_module._checksum_path(7) == transfer_module.STORAGE_DIR / "7.sha256"
