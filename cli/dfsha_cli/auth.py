"""
Sesión local y autenticación del cliente CLI.

Flujo: `dfsha login <usuario>` pide la contraseña (y el código 2FA si el
usuario lo activó), la manda a POST /auth/login del servidor y guarda el
JWT que devuelve en el archivo de sesión (~/.dfsha/session.json, con
permisos 600). A partir de ahí toda petición viaja con
`Authorization: Bearer <token>`, que es lo que construye
`build_auth_headers()` — el único punto de la CLI que sabe cómo se
autentica; client.py y los comandos de app.py no lo tocan.

Alternativa para scripts (Hito 3): `dfsha login --api-key` guarda una API
key en vez de un JWT y se manda como `X-API-Key`.

El token expira (12h por defecto, lo decide el servidor): cuando eso pasa
la API responde 401 y hay que volver a correr `dfsha login`.

Alta disponibilidad: `base_url` puede ser una lista separada por comas de
ControlNodes; el login prueba cada uno hasta que uno (el líder) responda.
"""
from __future__ import annotations

import json
import stat
from dataclasses import asdict, dataclass, fields

import httpx

from dfsha_cli.config import CONFIG_DIR, DEFAULT_BASE_URL, DEFAULT_CA_CERT, DEFAULT_TIMEOUT, SESSION_FILE


class NotLoggedInError(Exception):
    """No hay sesión activa (no se ha corrido `dfsha login`)."""


class LoginError(Exception):
    """El intento de login falló (p. ej. no se pudo contactar al servidor)."""


class OtpRequired(LoginError):
    """El usuario tiene 2FA: hay que repetir el login con el código."""


@dataclass
class Session:
    username: str
    base_url: str
    cwd: str = "/"
    # JWT emitido por POST /auth/login. Es lo que autentica cada petición.
    token: str | None = None
    # API key (alternativa al JWT para scripts).
    api_key: str | None = None
    # CA para verificar el TLS del clúster.
    ca_cert: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @property
    def base_urls(self) -> list[str]:
        return split_urls(self.base_url)

    @property
    def verify(self):
        return self.ca_cert or True


def split_urls(value: str) -> list[str]:
    return [url.strip().rstrip("/") for url in value.split(",") if url.strip()]


def _ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_session() -> Session:
    """Carga la sesión guardada. Lanza NotLoggedInError si no existe."""
    if not SESSION_FILE.exists():
        raise NotLoggedInError(
            "No hay sesión activa. Corre 'dfsha login <usuario>' primero."
        )
    data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    known = {f.name for f in fields(Session)}
    return Session(**{k: v for k, v in data.items() if k in known})


def save_session(session: Session) -> None:
    _ensure_config_dir()
    SESSION_FILE.write_text(session.to_json(), encoding="utf-8")
    # El archivo de sesión contiene el JWT o la API key: solo
    # lectura/escritura del dueño.
    SESSION_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)


def clear_session() -> bool:
    """Borra la sesión local. Devuelve True si había una sesión que borrar."""
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        return True
    return False


def _error_code(resp: httpx.Response) -> str | None:
    try:
        detail = resp.json().get("detail")
    except ValueError:
        return None
    return detail.get("error") if isinstance(detail, dict) else None


def login(
    username: str,
    password: str,
    base_url: str | None = None,
    otp: str | None = None,
    ca_cert: str | None = None,
) -> Session:
    """Inicia sesión contra el servidor: verifica las credenciales en
    POST /auth/login, guarda el JWT devuelto en la sesión local y la
    retorna. Lanza OtpRequired si falta el código 2FA y LoginError si las
    credenciales son inválidas o si no se puede contactar al servidor.
    """
    if not username or not username.strip():
        raise LoginError("El nombre de usuario no puede estar vacío")
    if not password:
        raise LoginError("La contraseña no puede estar vacía")

    urls = split_urls(base_url or DEFAULT_BASE_URL)
    verify = ca_cert or DEFAULT_CA_CERT or True
    body = {"username": username.strip(), "password": password}
    if otp:
        body["otp"] = otp.strip()

    resp = None
    last_error: Exception | None = None
    for url in urls:
        try:
            candidate = httpx.post(f"{url}/auth/login", json=body, timeout=DEFAULT_TIMEOUT, verify=verify)
        except httpx.HTTPError as exc:
            last_error = exc
            continue
        if candidate.status_code == 503 and _error_code(candidate) == "not_leader":
            continue  # respaldo: probar el siguiente ControlNode
        resp = candidate
        break
    if resp is None:
        raise LoginError(f"No se pudo contactar al servidor en {', '.join(urls)}: {last_error or 'ningún líder respondió'}")

    code = _error_code(resp)
    if resp.status_code == 401 and code == "otp_required":
        raise OtpRequired("Este usuario tiene 2FA activado: hace falta el código")
    if resp.status_code == 401 and code == "invalid_otp":
        raise LoginError("Código 2FA incorrecto o ya usado")
    if resp.status_code == 401:
        raise LoginError("Usuario o contraseña incorrectos")
    if resp.status_code == 429:
        raise LoginError("Demasiados intentos fallidos; espera unos minutos")
    if resp.status_code >= 400:
        raise LoginError(f"El servidor rechazó el login ({resp.status_code}): {resp.text}")

    try:
        data = resp.json()
        token = data["access_token"]
        resolved_username = data.get("username", username.strip())
    except (ValueError, KeyError) as exc:
        raise LoginError(f"Respuesta de login inesperada del servidor: {resp.text}") from exc

    session = Session(
        username=resolved_username,
        base_url=",".join(urls),
        cwd="/",
        token=token,
        ca_cert=ca_cert or DEFAULT_CA_CERT or None,
    )
    save_session(session)
    return session


def login_with_api_key(api_key: str, base_url: str | None = None, ca_cert: str | None = None) -> Session:
    """Sesión con API key: se comprueba contra el servidor y se guarda."""
    urls = split_urls(base_url or DEFAULT_BASE_URL)
    verify = ca_cert or DEFAULT_CA_CERT or True
    for url in urls:
        try:
            resp = httpx.get(f"{url}/auth/2fa", headers={"X-API-Key": api_key}, timeout=DEFAULT_TIMEOUT, verify=verify)
        except httpx.HTTPError:
            continue
        if resp.status_code == 503:
            continue
        if resp.status_code == 401:
            raise LoginError("API key inválida, revocada o expirada")
        if resp.status_code != 200:
            raise LoginError(f"El servidor rechazó la API key ({resp.status_code}): {resp.text}")
        session = Session(
            username=resp.json()["username"],
            base_url=",".join(urls),
            api_key=api_key,
            ca_cert=ca_cert or DEFAULT_CA_CERT or None,
        )
        save_session(session)
        return session
    raise LoginError(f"No se pudo contactar al servidor en {', '.join(urls)}")


def build_auth_headers(session: Session) -> dict[str, str]:
    """Headers de autenticación para toda request a la API: la API key, el
    JWT de la sesión o, en sesiones muy viejas, el X-Username heredado."""
    if session.api_key:
        return {"X-API-Key": session.api_key}
    if session.token:
        return {"Authorization": f"Bearer {session.token}"}
    return {"X-Username": session.username}
