"""
Sesión local y autenticación del cliente CLI.

Estado real del Hito 1 (IMPORTANTE, léase antes de tocar este archivo):
------------------------------------------------------------------------
El servidor (ver app/deps.py::get_current_user en el repo del backend)
NO implementa autenticación real todavía: cualquier request autenticado
solo necesita mandar un header `X-Username`, y el servidor autocrea ese
usuario si nunca lo había visto. No hay verificación de contraseña ni
JWT en el servidor en este hito.

Por eso `login()` en este módulo:
- SÍ pide username y password de forma interactiva (para que el flujo de
  usuario ya sea el definitivo y no haya que rehacer la UX después).
- NO envía ni valida la contraseña contra el servidor (no hay endpoint
  para eso todavía). La contraseña ni siquiera se guarda: solo se pide
  para que el comportamiento observable de "iniciar sesión" ya exista.
- Verifica conectividad contra el servidor (GET /health) para fallar
  rápido y con un mensaje claro si la base_url está mal, en vez de que
  el usuario descubra el problema recién en su primer `ls`.
- Guarda username, base_url y cwd en un archivo de sesión local
  (~/.dfsha/session.json).

Cuando el equipo implemente autenticación real (JWT), este es el único
archivo que hay que tocar: cambiar `login()` para que llame a un
endpoint real (p. ej. POST /auth/login) y guarde el token recibido en la
sesión, y cambiar `build_auth_headers()` para mandar
`Authorization: Bearer <token>` en vez de `X-Username`. El resto de la
CLI (client.py, commands en app.py) no debería necesitar cambios: todos
obtienen los headers de auth exclusivamente a través de
`build_auth_headers()`.
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
    # Reservado para cuando exista JWT real (ver docstring del módulo).
    # Hoy siempre es None y no se usa para autenticar.
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
    SESSION_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)


def clear_session() -> bool:
    """Borra la sesión local. Devuelve True si había una sesión que borrar."""
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        return True
    return False


def login(username: str, password: str, base_url: str | None = None) -> Session:
    """Inicia sesión: verifica conectividad con el servidor y guarda la
    sesión local. Ver el docstring del módulo para el porqué de que la
    contraseña no se use todavía.
    """
    del password  # No verificado por el servidor en el Hito 1 (ver docstring).

    if not username or not username.strip():
        raise LoginError("El nombre de usuario no puede estar vacío")

    resolved_base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

    try:
        resp = httpx.get(f"{resolved_base_url}/health", timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise LoginError(
            f"No se pudo contactar al servidor en {resolved_base_url}: {exc}"
        ) from exc

    session = Session(username=username.strip(), base_url=resolved_base_url, cwd="/")
    save_session(session)
    return session


def build_auth_headers(session: Session) -> dict[str, str]:
    """Headers de autenticación para toda request a la API.

    Hoy: solo X-Username (ver docstring del módulo). El día que haya JWT,
    este es el único lugar que cambia para el resto de la CLI.
    """
    if session.token:
        return {"Authorization": f"Bearer {session.token}"}
    return {"X-Username": session.username}
