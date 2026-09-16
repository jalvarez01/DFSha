"""
Sesión local y autenticación del cliente CLI.

Flujo: `dfsha login <usuario>` pide la contraseña, la manda a
POST /auth/login del servidor y guarda el JWT que devuelve en el archivo
de sesión (~/.dfsha/session.json, con permisos 600). A partir de ahí toda
petición viaja con `Authorization: Bearer <token>`, que es lo que
construye `build_auth_headers()` — el único punto de la CLI que sabe cómo
se autentica; client.py y los comandos de app.py no lo tocan.

El token expira (12h por defecto, lo decide el servidor): cuando eso pasa
la API responde 401 y hay que volver a correr `dfsha login`.
"""
from __future__ import annotations

import json
import stat
from dataclasses import asdict, dataclass

import httpx

from dfsha_cli.config import CONFIG_DIR, DEFAULT_BASE_URL, DEFAULT_TIMEOUT, SESSION_FILE


class NotLoggedInError(Exception):
    """No hay sesión activa (no se ha corrido `dfsha login`)."""


class LoginError(Exception):
    """El intento de login falló (p. ej. no se pudo contactar al servidor)."""


@dataclass
class Session:
    username: str
    base_url: str
    cwd: str = "/"
    # JWT emitido por POST /auth/login. Es lo que autentica cada petición.
    token: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def _ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_session() -> Session:
    """Carga la sesión guardada. Lanza NotLoggedInError si no existe."""
    if not SESSION_FILE.exists():
        raise NotLoggedInError(
            "No hay sesión activa. Corre 'dfsha login <usuario>' primero."
        )
    data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    return Session(**data)


def save_session(session: Session) -> None:
    _ensure_config_dir()
    SESSION_FILE.write_text(session.to_json(), encoding="utf-8")
    # Restringe permisos del archivo de sesión (contiene, a futuro, el
    # JWT): solo lectura/escritura para el dueño.
    # El archivo de sesión contiene el JWT: solo lectura/escritura del dueño.
    SESSION_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)


def clear_session() -> bool:
    """Borra la sesión local. Devuelve True si había una sesión que borrar."""
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        return True
    return False


def login(username: str, password: str, base_url: str | None = None) -> Session:
    """Inicia sesión contra el servidor: verifica las credenciales en
    POST /auth/login, guarda el JWT devuelto en la sesión local y la
    retorna. Lanza LoginError si las credenciales son inválidas o si no se
    puede contactar al servidor.
    """
    if not username or not username.strip():
        raise LoginError("El nombre de usuario no puede estar vacío")
    if not password:
        raise LoginError("La contraseña no puede estar vacía")

    resolved_base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

    try:
        resp = httpx.post(
            f"{resolved_base_url}/auth/login",
            json={"username": username.strip(), "password": password},
            timeout=DEFAULT_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise LoginError(
            f"No se pudo contactar al servidor en {resolved_base_url}: {exc}"
        ) from exc

    if resp.status_code == 401:
        raise LoginError("Usuario o contraseña incorrectos")
    if resp.status_code >= 400:
        raise LoginError(f"El servidor rechazó el login ({resp.status_code}): {resp.text}")

    try:
        data = resp.json()
        token = data["access_token"]
        resolved_username = data.get("username", username.strip())
    except (ValueError, KeyError) as exc:
        raise LoginError(f"Respuesta de login inesperada del servidor: {resp.text}") from exc

    session = Session(
        username=resolved_username, base_url=resolved_base_url, cwd="/", token=token
    )
    save_session(session)
    return session


def build_auth_headers(session: Session) -> dict[str, str]:
    """Headers de autenticación para toda request a la API: el JWT de la
    sesión. El fallback a X-Username cubre sesiones guardadas antes de que
    existiera el login con token (el servidor lo sigue aceptando).
    """
    if session.token:
        return {"Authorization": f"Bearer {session.token}"}
    return {"X-Username": session.username}
