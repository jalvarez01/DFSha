"""
Tests de autenticación: POST /auth/login y la resolución del usuario
actual a partir del JWT (app/deps.py::get_current_user).

Se monta un mini-app con el router de auth y una ruta de prueba que
depende de `get_current_user`, para verificar el token sin arrastrar los
routers de RF1/RF2.
"""
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app.routers import auth as auth_module
from app.security import decode_access_token, hash_password, verify_password


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(auth_module.router, prefix="/auth")

    @app.get("/whoami")
    def whoami(user: User = Depends(get_current_user)):
        return {"username": user.username, "id": user.id}

    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client


def _login(client, username: str, password: str):
    return client.post("/auth/login", json={"username": username, "password": password})


# --------------------------------------------------------------------------
# Hash de contraseñas
# --------------------------------------------------------------------------
def test_hash_password_es_distinto_cada_vez_por_el_salt():
    assert hash_password("secreta") != hash_password("secreta")


def test_verify_password_acepta_la_correcta_y_rechaza_el_resto():
    stored = hash_password("secreta")
    assert verify_password("secreta", stored) is True
    assert verify_password("otra", stored) is False


@pytest.mark.parametrize("stored", [None, "", "no-es-un-hash", "pbkdf2_sha256$abc"])
def test_verify_password_no_revienta_con_hashes_ausentes_o_corruptos(stored):
    assert verify_password("secreta", stored) is False


# --------------------------------------------------------------------------
# POST /auth/login
# --------------------------------------------------------------------------
def test_login_de_usuario_nuevo_lo_crea_y_devuelve_token(client, db):
    resp = _login(client, "mariana", "clave123")

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["username"] == "mariana"
    assert body["expires_in"] > 0

    payload = decode_access_token(body["access_token"])
    user = db.scalar(select(User).where(User.username == "mariana"))
    assert user is not None and user.password_hash is not None
    assert payload["sub"] == "mariana" and payload["uid"] == user.id


def test_login_repetido_con_la_misma_clave_funciona(client):
    assert _login(client, "mariana", "clave123").status_code == 200
    assert _login(client, "mariana", "clave123").status_code == 200


def test_login_con_clave_incorrecta_da_401(client):
    _login(client, "mariana", "clave123")

    resp = _login(client, "mariana", "otra-clave")

    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_credentials"


def test_primer_login_fija_la_clave_de_un_usuario_preexistente_sin_hash(client, db):
    # 'tester' lo siembra la fixture db sin password_hash.
    assert _login(client, "tester", "clave-nueva").status_code == 200
    assert _login(client, "tester", "clave-nueva").status_code == 200
    assert _login(client, "tester", "clave-mala").status_code == 401


def test_login_no_crea_usuarios_duplicados(client, db):
    _login(client, "mariana", "clave123")
    _login(client, "mariana", "clave123")

    assert db.query(User).filter_by(username="mariana").count() == 1


@pytest.mark.parametrize("payload", [{"username": "", "password": "x"}, {"username": "a"}, {}])
def test_login_con_payload_invalido_da_422(client, payload):
    assert client.post("/auth/login", json=payload).status_code == 422


# --------------------------------------------------------------------------
# Uso del token en endpoints protegidos
# --------------------------------------------------------------------------
def test_token_valido_identifica_al_usuario(client):
    token = _login(client, "mariana", "clave123").json()["access_token"]

    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.json()["username"] == "mariana"


def test_sin_ningun_header_de_auth_da_401(client):
    resp = client.get("/whoami")

    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "unauthenticated"


@pytest.mark.parametrize(
    "header",
    ["Bearer no-es-un-jwt", "Bearer ", "Token abc", "eyJhbGciOiJIUzI1NiJ9.abc.def"],
)
def test_token_malformado_o_esquema_incorrecto_da_401(client, header):
    resp = client.get("/whoami", headers={"Authorization": header})

    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_token"


def test_token_firmado_con_otro_secreto_da_401(client, monkeypatch):
    import jwt as pyjwt

    from app.config import settings

    token = pyjwt.encode({"sub": "mariana", "uid": 1}, "otro-secreto", algorithm=settings.jwt_algorithm)

    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 401


def test_token_de_un_usuario_borrado_da_401(client, db):
    token = _login(client, "mariana", "clave123").json()["access_token"]
    db.delete(db.scalar(select(User).where(User.username == "mariana")))
    db.commit()

    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_token"


def test_token_expirado_da_401(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "jwt_expire_minutes", -1)
    token = _login(client, "mariana", "clave123").json()["access_token"]

    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 401


def test_x_username_sigue_funcionando_como_fallback(client):
    """Los tests y scripts de RF1/RF2 siguen usándolo; no debe romperse."""
    resp = client.get("/whoami", headers={"X-Username": "jacobo"})

    assert resp.status_code == 200
    assert resp.json()["username"] == "jacobo"


def test_el_token_tiene_prioridad_sobre_x_username(client):
    token = _login(client, "mariana", "clave123").json()["access_token"]

    resp = client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {token}", "X-Username": "otro"},
    )

    assert resp.json()["username"] == "mariana"
