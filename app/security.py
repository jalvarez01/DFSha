"""
Primitivas de seguridad: hash de contraseñas y emisión/verificación de JWT.

Decisiones del Hito 1:

- Hash de contraseñas con PBKDF2-HMAC-SHA256 de la librería estándar
  (`hashlib`), no bcrypt/argon2. Motivo: evita una dependencia nativa que
  hay que compilar y que suele romper la instalación en las máquinas del
  equipo, sin renunciar a un KDF con salt e iteraciones. El formato del
  hash guardado es `pbkdf2_sha256$<iteraciones>$<salt_hex>$<hash_hex>`,
  autodescriptivo: si algún día se sube el número de iteraciones, los
  hashes viejos se siguen pudiendo verificar.
- JWT firmado con HS256 y un secreto de `settings.jwt_secret`. En
  producción ese secreto DEBE venir de la variable de entorno
  DFSHA_JWT_SECRET; el valor por defecto solo sirve para desarrollo.

El token lleva `sub` (username), `uid` (id del usuario, para no golpear
la BD por nombre), `iat` y `exp`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import jwt

from app.config import settings

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 200_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Devuelve el hash serializado de una contraseña, con salt aleatorio."""
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Verifica una contraseña contra el hash guardado.

    Devuelve False (en vez de lanzar) ante cualquier hash ausente o
    malformado: un registro corrupto no debe tumbar el endpoint de login,
    solo impedir el acceso. La comparación final es en tiempo constante.
    """
    if not stored:
        return False
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def create_access_token(*, username: str, user_id: int) -> tuple[str, int]:
    """Emite un JWT para el usuario. Devuelve (token, segundos_de_vida)."""
    expires_in = settings.jwt_expire_minutes * 60
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "uid": user_id,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_in


class InvalidTokenError(Exception):
    """El token no es válido (firma incorrecta, expirado o malformado)."""


def decode_access_token(token: str) -> dict:
    """Valida firma y expiración del token y devuelve su payload."""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc


# --------------------------------------------------------------------------
# 2FA: TOTP (RFC 6238), compatible con Google Authenticator, Authy, etc.
# Implementado con la librería estándar: HMAC-SHA1, pasos de 30 s, 6 dígitos.
# --------------------------------------------------------------------------
TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
# Se aceptan códigos del paso anterior y el siguiente (desfase de relojes).
TOTP_WINDOW = 1


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    padded = secret_b32 + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**TOTP_DIGITS)
    return str(code).zfill(TOTP_DIGITS)


def totp_now(secret_b32: str, at: float | None = None) -> str:
    return _hotp(secret_b32, int((at if at is not None else time.time()) // TOTP_STEP_SECONDS))


def verify_totp(secret_b32: str, code: str, last_counter: int = 0, at: float | None = None) -> int | None:
    """Devuelve el contador del paso que coincide (para guardarlo y no
    aceptar el mismo código dos veces), o None si el código no vale."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return None
    current = int((at if at is not None else time.time()) // TOTP_STEP_SECONDS)
    for counter in range(current - TOTP_WINDOW, current + TOTP_WINDOW + 1):
        if counter <= last_counter:
            continue
        if hmac.compare_digest(_hotp(secret_b32, counter), code):
            return counter
    return None


def totp_uri(secret_b32: str, username: str, issuer: str = "DFSha") -> str:
    label = quote(f"{issuer}:{username}", safe=":@")
    return f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer)}&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}"


# --------------------------------------------------------------------------
# API keys: "dfsha_<prefijo>_<secreto>". Se guarda solo el SHA-256 (la clave
# tiene 256 bits de entropía, así que un hash rápido es suficiente) y el
# prefijo, que permite buscarla sin recorrer todas.
# --------------------------------------------------------------------------
API_KEY_MARKER = "dfsha_"


def new_api_key() -> tuple[str, str, str]:
    """Devuelve (clave_completa, prefijo, hash)."""
    prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    key = f"{API_KEY_MARKER}{prefix}_{secret}"
    return key, prefix, hash_api_key(key)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def parse_api_key_prefix(key: str) -> str | None:
    if not key.startswith(API_KEY_MARKER):
        return None
    rest = key[len(API_KEY_MARKER) :]
    prefix, sep, secret = rest.partition("_")
    if not sep or not prefix or not secret:
        return None
    return prefix
