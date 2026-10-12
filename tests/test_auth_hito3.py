"""
Autenticación del Hito 3 (RNF6): 2FA con TOTP, API keys, bloqueo por fuerza
bruta y cuentas reservadas.
"""
import base64
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.security import totp_now, verify_totp


def _login(api, username="ana", password="clave", otp=None):
    body = {"username": username, "password": password}
    if otp:
        body["otp"] = otp
    return api.post("/auth/login", json=body)


def _bearer(api, username="ana", password="clave", otp=None):
    resp = _login(api, username, password, otp)
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# --------------------------------------------------------------------------
# TOTP
# --------------------------------------------------------------------------
def test_totp_cumple_los_vectores_del_rfc_6238():
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp_now(secret, at=59) == "287082"
    assert totp_now(secret, at=1111111109) == "081804"
    assert totp_now(secret, at=2000000000) == "279037"


def test_totp_acepta_un_paso_de_desfase_y_rechaza_reutilizar():
    secret = base64.b32encode(b"12345678901234567890").decode()
    previous = totp_now(secret, at=1000 - 30)
    counter = verify_totp(secret, previous, at=1000)
    assert counter is not None
    assert verify_totp(secret, previous, last_counter=counter, at=1000) is None
    assert verify_totp(secret, "000000", at=1000) is None
    assert verify_totp(secret, "abc", at=1000) is None


# --------------------------------------------------------------------------
# 2FA de punta a punta
# --------------------------------------------------------------------------
@pytest.fixture()
def with_2fa(api):
    headers = _bearer(api)
    secret = api.post("/auth/2fa/setup", headers=headers).json()["secret"]
    # El código del paso anterior: así el del paso actual sigue libre para el login.
    code = totp_now(secret, at=time.time() - 30)
    resp = api.post("/auth/2fa/enable", json={"code": code}, headers=headers)
    assert resp.status_code == 200 and resp.json()["enabled"] is True
    return secret


def test_setup_devuelve_secreto_y_uri(api):
    data = api.post("/auth/2fa/setup", headers=_bearer(api)).json()
    assert data["otpauth_uri"].startswith("otpauth://totp/DFSha:ana?secret=")
    assert len(data["secret"]) >= 32


def test_activar_con_codigo_incorrecto_es_400(api):
    headers = _bearer(api)
    api.post("/auth/2fa/setup", headers=headers)
    assert api.post("/auth/2fa/enable", json={"code": "000000"}, headers=headers).status_code == 400


def test_con_2fa_el_login_exige_el_codigo(api, with_2fa):
    resp = _login(api)
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "otp_required"


def test_con_2fa_y_codigo_correcto_entra(api, with_2fa):
    assert _login(api, otp=totp_now(with_2fa)).status_code == 200


def test_con_2fa_un_codigo_no_se_puede_reutilizar(api, with_2fa):
    code = totp_now(with_2fa)
    assert _login(api, otp=code).status_code == 200
    resp = _login(api, otp=code)
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_otp"


def test_con_2fa_la_contrasenia_sigue_siendo_obligatoria(api, with_2fa):
    resp = _login(api, password="mala", otp=totp_now(with_2fa))
    assert resp.json()["detail"]["error"] == "invalid_credentials"


def test_desactivar_2fa(api, with_2fa):
    headers = _bearer(api, otp=totp_now(with_2fa))
    # El código del login ya se usó; el siguiente paso todavía no llegó:
    # se usa el contador siguiente simulando el reloj.
    code = totp_now(with_2fa, at=time.time() + 30)
    assert api.post("/auth/2fa/disable", json={"code": code}, headers=headers).json()["enabled"] is False
    assert _login(api).status_code == 200


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------
def _new_key(api, headers, name="script", **extra):
    resp = api.post("/auth/api-keys", json={"name": name, **extra}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_api_key_autentica_por_header_y_por_bearer(api):
    key = _new_key(api, _bearer(api))["key"]
    assert key.startswith("dfsha_")
    assert api.get("/auth/2fa", headers={"X-API-Key": key}).json()["username"] == "ana"
    assert api.get("/auth/2fa", headers={"Authorization": f"Bearer {key}"}).json()["username"] == "ana"


def test_solo_se_guarda_el_hash_de_la_clave(api, db):
    from app.models import ApiKey

    key = _new_key(api, _bearer(api))["key"]
    row = db.query(ApiKey).one()
    assert key not in (row.key_hash, row.prefix)
    assert "key" not in api.get("/auth/api-keys", headers={"X-API-Key": key}).json()[0]


def test_api_key_revocada_no_sirve(api):
    headers = _bearer(api)
    data = _new_key(api, headers)
    assert api.delete(f"/auth/api-keys/{data['id']}", headers=headers).json()["revoked"] is True
    resp = api.get("/auth/2fa", headers={"X-API-Key": data["key"]})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_api_key"


def test_api_key_inventada_no_sirve(api):
    _new_key(api, _bearer(api))
    assert api.get("/auth/2fa", headers={"X-API-Key": "dfsha_000000000000_inventada"}).status_code == 401


def test_api_key_expirada_no_sirve(api, db):
    from app.models import ApiKey

    key = _new_key(api, _bearer(api), expires_in_days=1)["key"]
    row = db.query(ApiKey).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert api.get("/auth/2fa", headers={"X-API-Key": key}).status_code == 401


def test_con_api_key_no_se_crean_mas_claves_ni_se_toca_2fa(api):
    key = _new_key(api, _bearer(api))["key"]
    headers = {"X-API-Key": key}
    assert api.post("/auth/api-keys", json={"name": "otra"}, headers=headers).status_code == 403
    assert api.post("/auth/2fa/setup", headers=headers).status_code == 403


def test_no_se_puede_revocar_la_clave_de_otro(api):
    data = _new_key(api, _bearer(api))
    other = _bearer(api, username="bob", password="otra")
    assert api.delete(f"/auth/api-keys/{data['id']}", headers=other).status_code == 404


# --------------------------------------------------------------------------
# Fuerza bruta y cuentas reservadas
# --------------------------------------------------------------------------
def test_bloqueo_tras_varios_fallos_seguidos(api, monkeypatch):
    monkeypatch.setattr(settings, "max_login_failures", 3)
    _login(api)  # fija la contraseña
    for _ in range(3):
        assert _login(api, password="mala").status_code == 401
    resp = _login(api)  # ni siquiera con la buena
    assert resp.status_code == 429
    assert resp.json()["detail"]["error"] == "too_many_attempts"


def test_un_login_correcto_reinicia_el_contador(api, monkeypatch):
    monkeypatch.setattr(settings, "max_login_failures", 3)
    _login(api)
    _login(api, password="mala")
    _login(api, password="mala")
    assert _login(api).status_code == 200
    _login(api, password="mala")
    _login(api, password="mala")
    assert _login(api).status_code == 200


@pytest.mark.parametrize("reserved", ["system", "replicator"])
def test_las_cuentas_internas_no_pueden_iniciar_sesion(api, reserved):
    assert _login(api, username=reserved, password="cualquiera").status_code == 401
