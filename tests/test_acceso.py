"""
Control de acceso (Hito 3, RNF6): permisos estilo Unix, sticky bit de la
raíz, grupos, ACL, chmod/chgrp y la autenticación por X-Username apagada
fuera de los tests.
"""
import pytest

from app.config import settings
from tests.conftest import as_user
from tests.helpers import register_n, write_file

ANA, BOB, CAROL = as_user("ana"), as_user("bob"), as_user("carol")


def _mkdir(api, path, who):
    return api.post("/fs/mkdir", json={"path": path}, headers=who)


def _put(api, path, data, who):
    return api.put(f"/files{path}", content=data, headers=who)


def _get(api, path, who):
    return api.get(f"/files{path}", headers=who)


def _chmod(api, path, mode, who):
    return api.post("/fs/chmod", json={"path": path, "mode": mode}, headers=who)


@pytest.fixture()
def shared(api):
    """ana tiene /ana (0700) con /ana/doc.txt (0600); /pub es 0755 de ana.
    bob y carol ya existen (los usuarios se crean en su primera petición)."""
    for who in (BOB, CAROL):
        api.get("/fs/ls", params={"path": "/"}, headers=who)
    assert _mkdir(api, "/ana", ANA).status_code == 201
    assert _put(api, "/ana/doc.txt", b"secreto", ANA).status_code == 200
    _mkdir(api, "/pub", ANA)
    _chmod(api, "/pub", "755", ANA)
    return api


# --------------------------------------------------------------------------
# Defaults: privado
# --------------------------------------------------------------------------
def test_lo_que_crea_un_usuario_es_privado_por_defecto(shared):
    stat = shared.get("/fs/stat", params={"path": "/ana/doc.txt"}, headers=ANA).json()
    assert stat["owner"] == "ana"
    assert stat["mode"] == "-rw-------"
    assert shared.get("/fs/stat", params={"path": "/ana"}, headers=ANA).json()["mode"] == "drwx------"


def test_la_raiz_es_1777(api):
    stat = api.get("/fs/stat", params={"path": "/"}, headers=ANA).json()
    assert stat["mode_octal"] == "1777"
    assert stat["mode"] == "drwxrwxrwt"


def test_otro_usuario_no_puede_leer_ni_listar(shared):
    assert _get(shared, "/ana/doc.txt", BOB).status_code == 403
    assert shared.get("/fs/ls", params={"path": "/ana"}, headers=BOB).status_code == 403
    assert shared.get("/fs/stat", params={"path": "/ana/doc.txt"}, headers=BOB).status_code == 403


def test_otro_usuario_no_puede_escribir_ni_crear_dentro(shared):
    assert _put(shared, "/ana/doc.txt", b"pisado", BOB).status_code == 403
    assert _put(shared, "/ana/nuevo.txt", b"x", BOB).status_code == 403
    assert _mkdir(shared, "/ana/sub", BOB).status_code == 403


def test_otro_usuario_no_puede_borrar(shared):
    assert shared.request("DELETE", "/fs/rm", json={"path": "/ana/doc.txt"}, headers=BOB).status_code == 403
    assert shared.request("DELETE", "/fs/rmdir", json={"path": "/ana", "recursive": True}, headers=BOB).status_code == 403
    assert _get(shared, "/ana/doc.txt", ANA).content == b"secreto"


def test_sticky_bit_en_la_raiz(api):
    """Todos crean en /, pero solo el dueño borra lo suyo (como /tmp)."""
    _put(api, "/de-ana.txt", b"a", ANA)
    assert _put(api, "/de-bob.txt", b"b", BOB).status_code == 200
    resp = api.request("DELETE", "/fs/rm", json={"path": "/de-ana.txt"}, headers=BOB)
    assert resp.status_code == 403
    assert "sticky" in resp.json()["detail"]["message"]
    assert api.request("DELETE", "/fs/rm", json={"path": "/de-bob.txt"}, headers=BOB).status_code == 200


def test_x_hace_falta_en_todos_los_ancestros(shared):
    # /pub es 755 pero /pub/priv (700) no: bob no llega a lo de dentro.
    _mkdir(shared, "/pub/priv", ANA)
    _put(shared, "/pub/priv/x.txt", b"x", ANA)
    _chmod(shared, "/pub/priv/x.txt", "644", ANA)
    assert _get(shared, "/pub/priv/x.txt", BOB).status_code == 403
    _chmod(shared, "/pub/priv", "711", ANA)  # x para otros, sin r
    assert _get(shared, "/pub/priv/x.txt", BOB).status_code == 200
    assert shared.get("/fs/ls", params={"path": "/pub/priv"}, headers=BOB).status_code == 403


# --------------------------------------------------------------------------
# chmod / chgrp / grupos
# --------------------------------------------------------------------------
def test_chmod_comparte_con_otros(shared):
    _chmod(shared, "/ana", "755", ANA)
    _chmod(shared, "/ana/doc.txt", "644", ANA)
    assert _get(shared, "/ana/doc.txt", BOB).content == b"secreto"
    assert _put(shared, "/ana/doc.txt", b"x", BOB).status_code == 403


def test_solo_el_duenio_hace_chmod(shared):
    resp = _chmod(shared, "/ana/doc.txt", "777", BOB)
    assert resp.status_code == 403


def test_chmod_con_modo_invalido_es_400(shared):
    assert _chmod(shared, "/ana/doc.txt", "9z", ANA).status_code == 400


def test_compartir_por_grupo(shared):
    shared.post("/groups", json={"name": "equipo"}, headers=ANA)
    shared.post("/groups/equipo/members", json={"username": "bob"}, headers=ANA)
    # carol no está en el grupo.
    for path in ("/ana", "/ana/doc.txt"):
        assert shared.post("/fs/chgrp", json={"path": path, "group": "equipo"}, headers=ANA).status_code == 200
    _chmod(shared, "/ana", "750", ANA)
    _chmod(shared, "/ana/doc.txt", "640", ANA)
    assert _get(shared, "/ana/doc.txt", BOB).content == b"secreto"
    assert _get(shared, "/ana/doc.txt", CAROL).status_code == 403
    listing = shared.get("/fs/ls", params={"path": "/ana"}, headers=BOB).json()
    assert listing["entries"][0]["group"] == "equipo"


def test_lo_nuevo_hereda_el_grupo_del_directorio(shared):
    shared.post("/groups", json={"name": "equipo"}, headers=ANA)
    shared.post("/fs/chgrp", json={"path": "/ana", "group": "equipo"}, headers=ANA)
    _put(shared, "/ana/otro.txt", b"x", ANA)
    assert shared.get("/fs/stat", params={"path": "/ana/otro.txt"}, headers=ANA).json()["group"] == "equipo"


def test_chgrp_a_un_grupo_ajeno_es_403(shared):
    shared.post("/groups", json={"name": "de-bob"}, headers=BOB)
    assert shared.post("/fs/chgrp", json={"path": "/ana", "group": "de-bob"}, headers=ANA).status_code == 403


def test_solo_el_duenio_del_grupo_gestiona_miembros(api):
    api.post("/groups", json={"name": "equipo"}, headers=ANA)
    api.get("/fs/ls", params={"path": "/"}, headers=CAROL)
    assert api.post("/groups/equipo/members", json={"username": "carol"}, headers=BOB).status_code == 403
    assert api.post("/groups/equipo/members", json={"username": "carol"}, headers=ANA).json()["members"] == ["ana", "carol"]
    assert api.post("/groups", json={"name": "equipo"}, headers=BOB).status_code == 409


def test_borrar_un_grupo_quita_sus_permisos(shared):
    shared.post("/groups", json={"name": "equipo"}, headers=ANA)
    shared.post("/groups/equipo/members", json={"username": "bob"}, headers=ANA)
    shared.post("/fs/chgrp", json={"path": "/ana", "group": "equipo"}, headers=ANA)
    assert shared.delete("/groups/equipo", headers=ANA).status_code == 204
    assert shared.get("/fs/stat", params={"path": "/ana"}, headers=ANA).json()["group"] is None


# --------------------------------------------------------------------------
# ACL
# --------------------------------------------------------------------------
def test_acl_de_usuario_da_permisos_a_uno_solo(shared):
    _chmod(shared, "/ana", "711", ANA)
    resp = shared.post("/fs/acl", json={"path": "/ana/doc.txt", "type": "user", "name": "bob", "perms": "r--"}, headers=ANA)
    assert resp.status_code == 200
    assert resp.json()["entries"] == [{"type": "user", "name": "bob", "perms": "r--"}]
    assert resp.json()["mode"].endswith("+")
    assert _get(shared, "/ana/doc.txt", BOB).content == b"secreto"
    assert _get(shared, "/ana/doc.txt", CAROL).status_code == 403
    assert _put(shared, "/ana/doc.txt", b"x", BOB).status_code == 403


def test_acl_de_grupo(shared):
    shared.post("/groups", json={"name": "lectores"}, headers=ANA)
    shared.post("/groups/lectores/members", json={"username": "bob"}, headers=ANA)
    _chmod(shared, "/ana", "711", ANA)
    shared.post("/fs/acl", json={"path": "/ana/doc.txt", "type": "group", "name": "lectores", "perms": "rw"}, headers=ANA)
    assert _put(shared, "/ana/doc.txt", b"editado por bob", BOB).status_code == 200


def test_quitar_una_entrada_de_acl(shared):
    _chmod(shared, "/ana", "711", ANA)
    shared.post("/fs/acl", json={"path": "/ana/doc.txt", "type": "user", "name": "bob", "perms": "r"}, headers=ANA)
    resp = shared.request("DELETE", "/fs/acl", json={"path": "/ana/doc.txt", "type": "user", "name": "bob"}, headers=ANA)
    assert resp.json()["entries"] == []
    assert _get(shared, "/ana/doc.txt", BOB).status_code == 403


def test_solo_el_duenio_cambia_la_acl(shared):
    resp = shared.post("/fs/acl", json={"path": "/ana/doc.txt", "type": "user", "name": "bob", "perms": "rwx"}, headers=BOB)
    assert resp.status_code == 403


def test_acl_con_permisos_invalidos_es_400(shared):
    resp = shared.post("/fs/acl", json={"path": "/ana/doc.txt", "type": "user", "name": "ana", "perms": "rwq"}, headers=ANA)
    assert resp.status_code == 400


def test_borrar_el_archivo_borra_su_acl(shared, db):
    from app.models import AclEntry

    shared.post("/fs/acl", json={"path": "/ana/doc.txt", "type": "user", "name": "ana", "perms": "r"}, headers=ANA)
    shared.request("DELETE", "/fs/rm", json={"path": "/ana/doc.txt"}, headers=ANA)
    assert db.query(AclEntry).count() == 0


# --------------------------------------------------------------------------
# Archivos distribuidos: los planes también respetan los permisos
# --------------------------------------------------------------------------
def test_plan_de_lectura_y_escritura_respetan_permisos(api):
    register_n(api, 3)
    api.headers.update(ANA)
    write_file(api, "/privado.bin", 20)
    api.headers.pop("X-Username")
    assert api.get("/files/privado.bin/blocks", headers=BOB).status_code == 403
    assert api.post("/files/privado.bin/allocate", json={"size_bytes": 5}, headers=BOB).status_code == 403
    assert api.post("/files/privado.bin/blocks/0/allocate", json={"size_bytes": 5}, headers=BOB).status_code == 403
    assert api.get("/files/privado.bin/blocks", headers=ANA).status_code == 200


# --------------------------------------------------------------------------
# Superusuario y autenticación heredada
# --------------------------------------------------------------------------
def test_superusuario_se_salta_los_permisos(shared, monkeypatch):
    monkeypatch.setattr(settings, "superusers", "admin")
    assert _get(shared, "/ana/doc.txt", as_user("admin")).content == b"secreto"


def test_x_username_esta_apagado_fuera_de_desarrollo(api, monkeypatch):
    monkeypatch.setattr(settings, "allow_insecure_dev_auth", False)
    resp = api.get("/fs/ls", params={"path": "/"}, headers=ANA)
    assert resp.status_code == 401
