"""
Dependencias de FastAPI compartidas (además de `get_db`, que vive en
app/database.py por estar ligada al engine).

`get_current_user` resuelve el usuario dueño de cada operación y acepta,
en este orden:

1. `X-API-Key: dfsha_...` o `Authorization: Bearer dfsha_...` — API key
   (Hito 3), para scripts. Ver POST /auth/api-keys.
2. `Authorization: Bearer <jwt>` — el camino normal. El token lo emite
   POST /auth/login tras verificar usuario/contraseña (y el código 2FA si
   el usuario lo activó). Se valida firma y expiración, y el usuario debe
   existir en la BD.
3. `X-Username: <nombre>` — camino heredado del inicio del Hito 1, cuando
   todavía no había login. NO verifica nada, así que desde el Hito 3 solo
   se acepta con `DFSHA_ALLOW_INSECURE_DEV_AUTH=true` (tests y desarrollo):
   con control de acceso real permitiría suplantar a cualquiera.
"""
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import ApiKey, User
from app.security import (
    API_KEY_MARKER,
    InvalidTokenError,
    decode_access_token,
    hash_api_key,
    parse_api_key_prefix,
)

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={
        "error": "unauthenticated",
        "message": "Falta autenticación: manda 'Authorization: Bearer <token>' (POST /auth/login)",
    },
    headers={"WWW-Authenticate": "Bearer"},
)


def _invalid_token(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "invalid_token", "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _user_from_token(db: Session, authorization: str) -> User:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _invalid_token("El header Authorization debe ser 'Bearer <token>'")

    try:
        payload = decode_access_token(token.strip())
    except InvalidTokenError as exc:
        raise _invalid_token(f"Token inválido o expirado: {exc}") from exc

    user = db.get(User, payload.get("uid"))
    if user is None or user.username != payload.get("sub"):
        # El token es válido pero el usuario ya no existe (o le cambiaron el
        # nombre): no se puede seguir operando con él.
        raise _invalid_token("El usuario del token ya no existe")
    return user


def _as_utc(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _user_from_api_key(db: Session, key: str) -> User:
    prefix = parse_api_key_prefix(key)
    row = db.scalar(select(ApiKey).where(ApiKey.prefix == prefix)) if prefix else None
    if row is None or row.revoked or row.key_hash != hash_api_key(key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_api_key", "message": "API key inválida o revocada"},
        )
    now = datetime.now(timezone.utc)
    if row.expires_at is not None and _as_utc(row.expires_at) < now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_api_key", "message": "La API key expiró"},
        )
    row.last_used_at = now
    db.commit()
    return row.user


def _api_key_from_headers(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip().startswith(API_KEY_MARKER):
            return token.strip()
    return None


def get_current_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_username: str | None = Header(default=None, alias="X-Username"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> User:
    """Resuelve el usuario "actual" de la petición (ver docstring del módulo)."""
    api_key = _api_key_from_headers(authorization, x_api_key)
    if api_key:
        return _user_from_api_key(db, api_key)

    if authorization:
        return _user_from_token(db, authorization)

    if not x_username or not settings.allow_insecure_dev_auth:
        raise _UNAUTHENTICATED

    user = db.scalar(select(User).where(User.username == x_username))
    if user is None:
        user = User(username=x_username)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def get_session_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_username: str | None = Header(default=None, alias="X-Username"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> User:
    """Como get_current_user pero sin aceptar API keys: para operaciones
    sensibles (crear API keys, activar 2FA). Una API key filtrada no debe
    servir para fabricar más claves ni para tomar control de la cuenta."""
    if _api_key_from_headers(authorization, x_api_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "session_required",
                "message": "Esta operación exige iniciar sesión con usuario y contraseña, no con API key",
            },
        )
    return get_current_user(authorization, x_username, None, db)
