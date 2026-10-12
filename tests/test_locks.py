"""
Locks de archivo y leases de escritura (Hito 3: consistencia, RF3 lock()).
"""
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import as_user
from tests.helpers import register_n, store_block

ANA, BOB = as_user("ana"), as_user("bob")


@pytest.fixture()
def api3(api):
    register_n(api, 3)
    # Un archivo de ana, compartido con permiso de escritura para todos.
    plan = api.post("/files/f.bin/allocate", json={"size_bytes": 10}, headers=ANA).json()
    confirmation = store_block(api, plan["blocks"][0])
    api.post("/files/f.bin/confirm", json={"blocks": [confirmation], "lease_id": plan["lease_id"]}, headers=ANA)
    api.post("/fs/chmod", json={"path": "/f.bin", "mode": "666"}, headers=ANA)
    return api


def _lock(api, who, mode="exclusive", ttl=None):
    return api.post("/files/f.bin/lock", json={"mode": mode, "ttl_seconds": ttl}, headers=who)


def test_un_lease_de_escritura_bloquea_a_otro_escritor(api3):
    first = api3.post("/files/f.bin/allocate", json={"size_bytes": 5}, headers=ANA)
    assert first.status_code == 200
    resp = api3.post("/files/f.bin/allocate", json={"size_bytes": 5}, headers=BOB)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "file_locked"


def test_el_lease_se_libera_al_confirmar(api3):
    plan = api3.post("/files/f.bin/allocate", json={"size_bytes": 5}, headers=ANA).json()
    confirmation = store_block(api3, plan["blocks"][0])
    api3.post("/files/f.bin/confirm", json={"blocks": [confirmation], "lease_id": plan["lease_id"]}, headers=ANA)
    assert api3.get("/files/f.bin/locks", headers=ANA).json() == []
    assert api3.post("/files/f.bin/allocate", json={"size_bytes": 5}, headers=BOB).status_code == 200


def test_otro_usuario_no_puede_confirmar_mientras_alguien_escribe(api3):
    plan = api3.post("/files/f.bin/allocate", json={"size_bytes": 5}, headers=ANA).json()
    confirmation = store_block(api3, plan["blocks"][0])
    resp = api3.post("/files/f.bin/confirm", json={"blocks": [confirmation]}, headers=BOB)
    assert resp.status_code == 409


def test_lock_exclusivo_explicito(api3):
    lock = _lock(api3, ANA)
    assert lock.status_code == 201
    lock_id = lock.json()["lock_id"]
    # Nadie más escribe ni bloquea...
    assert api3.post("/files/f.bin/allocate", json={"size_bytes": 5}, headers=BOB).status_code == 409
    assert _lock(api3, BOB, "shared").status_code == 409
    assert api3.request("DELETE", "/fs/rm", json={"path": "/f.bin"}, headers=BOB).status_code == 403  # sticky
    # ...y el dueño escribe pasando su lock como lease.
    plan = api3.post("/files/f.bin/allocate", json={"size_bytes": 5, "lease_id": lock_id}, headers=ANA)
    assert plan.status_code == 200
    confirmation = store_block(api3, plan.json()["blocks"][0])
    api3.post("/files/f.bin/confirm", json={"blocks": [confirmation], "lease_id": lock_id}, headers=ANA)
    # El lock explícito sigue vigente tras confirmar.
    assert [l["lock_id"] for l in api3.get("/files/f.bin/locks", headers=ANA).json()] == [lock_id]


def test_con_lock_explicito_escribir_sin_pasarlo_choca(api3):
    _lock(api3, ANA)
    assert api3.post("/files/f.bin/allocate", json={"size_bytes": 5}, headers=ANA).status_code == 409


def test_locks_compartidos_conviven_y_bloquean_escritores(api3):
    assert _lock(api3, ANA, "shared").status_code == 201
    assert _lock(api3, BOB, "shared").status_code == 201
    assert _lock(api3, BOB, "exclusive").status_code == 409
    assert api3.post("/files/f.bin/allocate", json={"size_bytes": 5}, headers=BOB).status_code == 409


def test_un_lock_compartido_no_sirve_para_escribir(api3):
    lock_id = _lock(api3, ANA, "shared").json()["lock_id"]
    resp = api3.post("/files/f.bin/allocate", json={"size_bytes": 5, "lease_id": lock_id}, headers=ANA)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "invalid_lease"


def test_un_lock_ajeno_no_sirve_como_lease(api3):
    lock_id = _lock(api3, ANA).json()["lock_id"]
    resp = api3.post("/files/f.bin/allocate", json={"size_bytes": 5, "lease_id": lock_id}, headers=BOB)
    assert resp.json()["detail"]["error"] == "invalid_lease"


def test_liberar_y_renovar(api3):
    lock = _lock(api3, ANA, ttl=10).json()
    renewed = api3.post(f"/files/f.bin/lock/{lock['lock_id']}/renew", json={"ttl_seconds": 600}, headers=ANA).json()
    assert renewed["expires_at"] > lock["expires_at"]
    assert api3.delete(f"/files/f.bin/lock/{lock['lock_id']}", headers=BOB).status_code == 409
    assert api3.delete(f"/files/f.bin/lock/{lock['lock_id']}", headers=ANA).status_code == 200
    assert _lock(api3, BOB).status_code == 201


def test_un_lock_vencido_no_bloquea(api3, db):
    from app.models import FileLock

    _lock(api3, ANA)
    row = db.query(FileLock).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert _lock(api3, BOB).status_code == 201


def test_lock_exige_permisos(api3):
    api3.post("/fs/chmod", json={"path": "/f.bin", "mode": "644"}, headers=ANA)
    assert _lock(api3, BOB, "exclusive").status_code == 403  # sin w
    assert _lock(api3, BOB, "shared").status_code == 201  # con r


def test_rm_de_un_archivo_bloqueado_por_otro_es_409(api3):
    api3.post("/fs/mkdir", json={"path": "/comun"}, headers=ANA)
    api3.post("/fs/chmod", json={"path": "/comun", "mode": "777"}, headers=ANA)
    plan = api3.post("/files/comun/g.bin/allocate", json={"size_bytes": 5}, headers=BOB).json()
    assert plan["lease_id"]
    resp = api3.request("DELETE", "/fs/rmdir", json={"path": "/comun", "recursive": True}, headers=ANA)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "file_locked"
