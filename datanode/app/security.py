"""Autorización en el DataNode (Hito 3): tokens de bloque y de nodo.

El DataNode no sabe nada de usuarios, contraseñas ni ACL. Para leer o
escribir un bloque exige un **token de bloque** que firma el ControlNode
(con el secreto del clúster) después de comprobar los permisos del usuario
sobre el archivo, como los *block access tokens* de HDFS:

- `op=read`:  vale para GET /blocks/{bid}.
- `op=write`: vale para PUT /blocks/{bid}?file_id={fid}, y trae la cadena
  de réplica (`pipe`) con las direcciones internas de cada DataNode. Este
  nodo debe estar en ella; reenvía el bloque a los que le siguen. Como el
  token va firmado, un cliente no puede hacer que un DataNode reenvíe datos
  a una dirección arbitraria.

Los endpoints de administración (borrar a mano) exigen un token de nodo de
un ControlNode. Y este DataNode se identifica ante los ControlNodes con un
token de nodo propio (`node_headers`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Header, HTTPException, status

from app.config import settings

_ALGORITHM = "HS256"


def _unauthorized(error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": error, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer(authorization: str | None) -> str:
    if not authorization:
        raise _unauthorized("unauthenticated", "Falta el token (Authorization: Bearer <token de bloque>)")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("invalid_token", "El header Authorization debe ser 'Bearer <token>'")
    return token.strip()


def _decode(token: str, typ: str) -> dict:
    try:
        payload = jwt.decode(token, settings.cluster_secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise _unauthorized("invalid_token", f"Token inválido o expirado: {exc}") from exc
    if payload.get("typ") != typ:
        raise _unauthorized("invalid_token", f"Se esperaba un token de tipo {typ!r}")
    return payload


def _forbidden(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "forbidden", "message": message})


def block_token(authorization: str | None, block_id: str, op: str, file_id: str | None = None) -> dict:
    """Valida un token de bloque para `op` sobre `block_id`."""
    payload = _decode(_bearer(authorization), "block")
    if payload.get("bid") != block_id:
        raise _forbidden("El token es de otro bloque")
    if payload.get("op") != op:
        raise _forbidden(f"El token no permite la operación {op!r}")
    if file_id is not None and payload.get("fid") != file_id:
        raise _forbidden("El token es de otro archivo")
    if op == "write":
        pipe = payload.get("pipe") or []
        if settings.datanode_id not in [entry.get("node_id") for entry in pipe]:
            raise _forbidden(f"Este DataNode ({settings.datanode_id}) no está en la cadena del token")
    return payload


def require_controlnode(authorization: str | None = Header(default=None, alias="Authorization")) -> str:
    payload = _decode(_bearer(authorization), "node")
    if payload.get("role") != "controlnode":
        raise _forbidden("Solo un ControlNode puede usar este endpoint")
    return payload["sub"]


def node_headers() -> dict[str, str]:
    """Token con el que este DataNode se identifica ante un ControlNode."""
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "typ": "node",
            "sub": settings.datanode_id,
            "role": "datanode",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        settings.cluster_secret,
        algorithm=_ALGORITHM,
    )
    return {"Authorization": f"Bearer {token}"}
