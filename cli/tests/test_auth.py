"""
Tests de la sesión local y del login con JWT del cliente.

No levantan el servidor: se sustituye `httpx.post` por un doble que
devuelve la respuesta que interese, de modo que lo que se prueba es el
contrato del cliente (qué manda, qué guarda, cómo falla).

El archivo de sesión se redirige a un tmp_path en cada test para no tocar
el ~/.dfsha real de quien corra la suite.
"""
import json

import httpx
import pytest

from dfsha_cli import auth


@pytest.fixture(autouse=True)
def session_file(tmp_path, monkeypatch):
    """Aísla el archivo de sesión de cada test."""
    path = tmp_path / "session.json"
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth, "SESSION_FILE", path)
    return path


def _fake_post(status_code: int, payload: dict | None = None, text: str = ""):
    """Devuelve un reemplazo de httpx.post que responde siempre lo mismo y
    registra la última llamada en `.calls`."""

    def post(url, **kwargs):
        post.calls.append((url, kwargs))
        return httpx.Response(
            status_code,
            json=payload if payload is not None else None,
            text=None if payload is not None else text,
            request=httpx.Request("POST", url),
        )

    post.calls = []
    return post


_OK = {"access_token": "jwt-de-prueba", "token_type": "bearer", "expires_in": 43200, "username": "mariana"}


class TestLogin:
    def test_guarda_el_token_devuelto_por_el_servidor(self, monkeypatch, session_file):
        monkeypatch.setattr(httpx, "post", _fake_post(200, _OK))

        session = auth.login("mariana", "clave123", base_url="http://localhost:8000")

        assert session.token == "jwt-de-prueba"
        assert session.username == "mariana"
        assert session.cwd == "/"
        assert json.loads(session_file.read_text())["token"] == "jwt-de-prueba"

    def test_manda_usuario_y_clave_a_auth_login(self, monkeypatch):
        post = _fake_post(200, _OK)
        monkeypatch.setattr(httpx, "post", post)

        auth.login("mariana", "clave123", base_url="http://localhost:8000/")

        url, kwargs = post.calls[0]
        assert url == "http://localhost:8000/auth/login"
        assert kwargs["json"] == {"username": "mariana", "password": "clave123"}

    def test_credenciales_incorrectas_lanzan_login_error(self, monkeypatch, session_file):
        monkeypatch.setattr(httpx, "post", _fake_post(401, {"detail": {"error": "invalid_credentials"}}))

        with pytest.raises(auth.LoginError, match="incorrectos"):
            auth.login("mariana", "mala")

        assert not session_file.exists()

    def test_servidor_inalcanzable_lanza_login_error(self, monkeypatch):
        def boom(url, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "post", boom)

        with pytest.raises(auth.LoginError, match="No se pudo contactar"):
            auth.login("mariana", "clave123")

    def test_respuesta_sin_token_lanza_login_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", _fake_post(200, {"algo": "raro"}))

        with pytest.raises(auth.LoginError, match="inesperada"):
            auth.login("mariana", "clave123")

    @pytest.mark.parametrize(
        "username,password", [("", "clave"), ("   ", "clave"), ("mariana", "")]
    )
    def test_credenciales_vacias_ni_llegan_al_servidor(self, monkeypatch, username, password):
        post = _fake_post(200, _OK)
        monkeypatch.setattr(httpx, "post", post)

        with pytest.raises(auth.LoginError):
            auth.login(username, password)

        assert post.calls == []


class TestSession:
    def test_load_session_sin_archivo_lanza_not_logged_in(self):
        with pytest.raises(auth.NotLoggedInError):
            auth.load_session()

    def test_save_y_load_conservan_el_token(self):
        auth.save_session(auth.Session(username="m", base_url="http://x", cwd="/a", token="t"))

        loaded = auth.load_session()

        assert (loaded.username, loaded.cwd, loaded.token) == ("m", "/a", "t")

    def test_clear_session_borra_y_reporta(self):
        auth.save_session(auth.Session(username="m", base_url="http://x"))

        assert auth.clear_session() is True
        assert auth.clear_session() is False


class TestBuildAuthHeaders:
    def test_usa_bearer_cuando_hay_token(self):
        session = auth.Session(username="m", base_url="http://x", token="jwt")

        assert auth.build_auth_headers(session) == {"Authorization": "Bearer jwt"}

    def test_cae_a_x_username_en_sesiones_viejas_sin_token(self):
        session = auth.Session(username="m", base_url="http://x")

        assert auth.build_auth_headers(session) == {"X-Username": "m"}
