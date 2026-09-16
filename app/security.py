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

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

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
