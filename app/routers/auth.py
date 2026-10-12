"""
Autenticación: login con usuario/contraseña (y 2FA) que devuelve un JWT,
gestión de 2FA (TOTP) y de API keys.

Es la única puerta de entrada para obtener un token; el resto de la API lo
consume a través de `get_current_user` (app/deps.py).

Sobre el registro de usuarios: el enunciado pide "autenticación básica
(user/pass)" y no un flujo de alta de usuarios, así que no hay endpoint de
registro. En su lugar, el primer login de un username sin contraseña fija
la que se haya enviado (trust on first use) y los logins siguientes la
verifican.

Hito 3 (RNF6):
- 2FA opcional por usuario (TOTP, RFC 6238): /auth/2fa/setup genera el
  secreto, /auth/2fa/enable lo activa tras verificar un código, y desde ahí
  el login exige `otp`. Un código no se puede reutilizar.
- API keys para scripts: /auth/api-keys. Solo se guarda su hash.
- Bloqueo temporal de un usuario tras `max_login_failures` intentos
  fallidos seguidos (fuerza bruta). Es por ControlNode y en memoria: tras un
  failover el contador vuelve a cero, lo que solo da unos intentos extra.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user, get_session_user
from app.models import ApiKey, User
from app.schemas import (
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyInfo,
    LoginRequest,
    TokenResponse,
    TwoFactorCodeRequest,
    TwoFactorSetupResponse,
    TwoFactorStatusResponse,
)
from app.security import (
    create_access_token,
    hash_password,
    new_api_key,
    new_totp_secret,
    totp_uri,
    verify_password,
    verify_totp,
)

router = APIRouter()

# Cuentas internas que nunca pueden iniciar sesión: 'system' es el dueño de
# la raíz "/" (si alguien la reclamara en un primer login, podría cambiar
# los permisos de todo el árbol).
RESERVED_USERNAMES = frozenset({"system", "replicator"})

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={"error": "invalid_credentials", "message": "Usuario o contraseña incorrectos"},
    headers={"WWW-Authenticate": "Bearer"},
)


class _LoginThrottle:
    """Cuenta fallos seguidos por username y bloquea temporalmente."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}

    def check(self, username: str) -> None:
        with self._lock:
            until = self._locked_until.get(username, 0.0)
        remaining = until - time.monotonic()
        if remaining > 0:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "too_many_attempts",
                    "message": f"Demasiados intentos fallidos; espera {int(remaining) + 1} s",
                },
            )

    def failure(self, username: str) -> None:
        with self._lock:
            count = self._failures.get(username, 0) + 1
            if count >= settings.max_login_failures:
                self._locked_until[username] = time.monotonic() + settings.login_lockout_seconds
                count = 0
            self._failures[username] = count

    def success(self, username: str) -> None:
        with self._lock:
            self._failures.pop(username, None)
            self._locked_until.pop(username, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._locked_until.clear()


login_throttle = _LoginThrottle()


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Verifica las credenciales (y el código 2FA si está activo) y emite un JWT.

    Responde siempre 401 con el mismo mensaje si usuario o contraseña
    fallan, para no filtrar qué usernames existen.
    """
    username = payload.username.strip()
    if not username or username in RESERVED_USERNAMES:
        raise _INVALID_CREDENTIALS
    login_throttle.check(username)

    user = db.scalar(select(User).where(User.username == username))

    if user is None:
        # Usuario nuevo: se crea con la contraseña enviada.
        user = User(username=username, password_hash=hash_password(payload.password))
        db.add(user)
        db.commit()
        db.refresh(user)
    elif user.password_hash is None:
        # Usuario preexistente sin contraseña (creado vía X-Username en
        # desarrollo): este login se la fija.
        user.password_hash = hash_password(payload.password)
        db.commit()
        db.refresh(user)
    elif not verify_password(payload.password, user.password_hash):
        login_throttle.failure(username)
        raise _INVALID_CREDENTIALS

    if user.totp_enabled:
        if not payload.otp:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "otp_required", "message": "Este usuario tiene 2FA: manda también el código (otp)"},
            )
        counter = verify_totp(user.totp_secret or "", payload.otp, user.totp_last_counter)
        if counter is None:
            login_throttle.failure(username)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "invalid_otp", "message": "Código 2FA incorrecto o ya usado"},
            )
        user.totp_last_counter = counter
        db.commit()

    login_throttle.success(username)
    token, expires_in = create_access_token(username=user.username, user_id=user.id)
    return TokenResponse(access_token=token, expires_in=expires_in, username=user.username)


# --------------------------------------------------------------------------
# 2FA
# --------------------------------------------------------------------------
@router.get("/2fa", response_model=TwoFactorStatusResponse)
def two_factor_status(user: User = Depends(get_current_user)) -> TwoFactorStatusResponse:
    return TwoFactorStatusResponse(username=user.username, enabled=user.totp_enabled)


@router.post("/2fa/setup", response_model=TwoFactorSetupResponse)
def two_factor_setup(db: Session = Depends(get_db), user: User = Depends(get_session_user)) -> TwoFactorSetupResponse:
    """Genera un secreto TOTP nuevo (todavía inactivo hasta /2fa/enable)."""
    if user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "2fa_already_enabled", "message": "2FA ya está activo; desactívalo antes de regenerarlo"},
        )
    secret = new_totp_secret()
    user.totp_secret = secret
    user.totp_last_counter = 0
    db.commit()
    return TwoFactorSetupResponse(secret=secret, otpauth_uri=totp_uri(secret, user.username))


@router.post("/2fa/enable", response_model=TwoFactorStatusResponse)
def two_factor_enable(
    payload: TwoFactorCodeRequest, db: Session = Depends(get_db), user: User = Depends(get_session_user)
) -> TwoFactorStatusResponse:
    if not user.totp_secret:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "2fa_not_setup", "message": "Primero llama a /auth/2fa/setup"},
        )
    counter = verify_totp(user.totp_secret, payload.code, user.totp_last_counter)
    if counter is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_otp", "message": "Código 2FA incorrecto"},
        )
    user.totp_enabled = True
    user.totp_last_counter = counter
    db.commit()
    return TwoFactorStatusResponse(username=user.username, enabled=True)


@router.post("/2fa/disable", response_model=TwoFactorStatusResponse)
def two_factor_disable(
    payload: TwoFactorCodeRequest, db: Session = Depends(get_db), user: User = Depends(get_session_user)
) -> TwoFactorStatusResponse:
    if not user.totp_enabled:
        return TwoFactorStatusResponse(username=user.username, enabled=False)
    counter = verify_totp(user.totp_secret or "", payload.code, user.totp_last_counter)
    if counter is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_otp", "message": "Código 2FA incorrecto"},
        )
    user.totp_enabled = False
    user.totp_secret = None
    user.totp_last_counter = counter
    db.commit()
    return TwoFactorStatusResponse(username=user.username, enabled=False)


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------
def _key_info(row: ApiKey) -> ApiKeyInfo:
    return ApiKeyInfo(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked=row.revoked,
    )


@router.post("/api-keys", response_model=ApiKeyCreateResponse, status_code=201)
def create_api_key(
    payload: ApiKeyCreateRequest, db: Session = Depends(get_db), user: User = Depends(get_session_user)
) -> ApiKeyCreateResponse:
    key, prefix, key_hash = new_api_key()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days) if payload.expires_in_days else None
    )
    row = ApiKey(user_id=user.id, name=payload.name, prefix=prefix, key_hash=key_hash, expires_at=expires_at)
    db.add(row)
    db.commit()
    db.refresh(row)
    return ApiKeyCreateResponse(**_key_info(row).model_dump(), key=key)


@router.get("/api-keys", response_model=list[ApiKeyInfo])
def list_api_keys(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[ApiKeyInfo]:
    rows = db.scalars(select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.id))
    return [_key_info(row) for row in rows]


@router.delete("/api-keys/{key_id}", response_model=ApiKeyInfo)
def revoke_api_key(key_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> ApiKeyInfo:
    row = db.get(ApiKey, key_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": f"No tienes una API key con id {key_id}"},
        )
    row.revoked = True
    db.commit()
    db.refresh(row)
    return _key_info(row)
