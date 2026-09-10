"""
Dependencias de FastAPI compartidas (además de `get_db`, que vive en
app/database.py por estar ligada al engine).

`get_current_user` resuelve el usuario dueño de cada operación y acepta
dos formas de identificarse, en este orden:

1. `Authorization: Bearer <jwt>` — el camino real. El token lo emite
   POST /auth/login tras verificar usuario/contraseña (ver
   app/routers/auth.py). Se valida firma y expiración, y el usuario debe
   existir en la BD.
2. `X-Username: <nombre>` — camino heredado del inicio del Hito 1, cuando
   todavía no había login. NO verifica nada: autocrea el usuario si no
   existe. Se mantiene porque es lo que usan los tests y scripts de los
   demás módulos, y desaparece en el Hito 3 junto con el control de acceso
   real. No debe usarse fuera de desarrollo.
"""
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.security import InvalidTokenError, decode_access_token

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


def get_current_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_username: str | None = Header(default=None, alias="X-Username"),
    db: Session = Depends(get_db),
) -> User:
    """Resuelve el usuario "actual" de la petición (ver docstring del módulo)."""
    if authorization:
        return _user_from_token(db, authorization)

    if not x_username:
        raise _UNAUTHENTICATED

    user = db.scalar(select(User).where(User.username == x_username))
    if user is None:
        user = User(username=x_username)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user
