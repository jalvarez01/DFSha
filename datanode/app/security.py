"""Validación del JWT de usuario en el DataNode.

El cliente obtiene el token del ControlNode (POST /auth/login) y lo manda
también a cada DataNode (`Authorization: Bearer <jwt>`). Como ambos
comparten el secreto (DFSHA_JWT_SECRET), el DataNode valida firma y
expiración localmente, sin consultar al ControlNode en cada bloque.

Alcance del Hito 2: esto garantiza que solo usuarios autenticados lean o
escriban bloques. La autorización por archivo/bloque (que el usuario sea
dueño, tokens de bloque tipo HDFS) es del Hito 3.
"""
from __future__ import annotations

import jwt
from fastapi import Header, HTTPException, status

from app.config import settings


def _unauthorized(error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": error, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_user(authorization: str | None = Header(default=None, alias="Authorization")) -> str:
    """Dependencia de FastAPI: exige un JWT válido y devuelve el username."""
    if not authorization:
        raise _unauthorized(
            "unauthenticated",
            "Falta autenticación: manda 'Authorization: Bearer <token>' (token de POST /auth/login del ControlNode)",
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("invalid_token", "El header Authorization debe ser 'Bearer <token>'")
    try:
        payload = jwt.decode(token.strip(), settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise _unauthorized("invalid_token", f"Token inválido o expirado: {exc}") from exc
    username = payload.get("sub")
    if not username:
        raise _unauthorized("invalid_token", "El token no identifica a ningún usuario")
    return username
