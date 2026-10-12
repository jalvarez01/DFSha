"""
Autenticación entre nodos y tokens de bloque (Hito 3, RNF6).

Todo se firma con `settings.cluster_secret` (HS256), un secreto que
comparten los nodos del clúster y que es DISTINTO del `jwt_secret` de los
usuarios: un token de usuario nunca sirve para hacerse pasar por un nodo.

Tres tipos de token, distinguidos por el claim `typ`:

- `node`: identifica a un nodo ante otro (DataNode -> ControlNode en
  register/heartbeat/informes, ControlNode -> ControlNode en la réplica de
  metadatos). Vida corta (5 min); se genera uno nuevo en cada llamada.
- `block`: lo emite el ControlNode al cliente, por bloque, tras comprobar
  los permisos del usuario sobre el archivo. Es lo único que acepta el
  DataNode para leer o escribir un bloque (como los *block access tokens*
  de HDFS): el DataNode no necesita saber nada de usuarios ni de ACL.
  Para escrituras incluye la cadena de réplica (`pipe`) con las
  direcciones internas de cada DataNode; como va firmada, un cliente no
  puede hacer que un DataNode reenvíe datos a una dirección arbitraria.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Header, HTTPException, status

from app.config import settings

_ALGORITHM = "HS256"
NODE_TOKEN_TTL = timedelta(minutes=5)


class ClusterTokenError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(payload: dict, ttl: timedelta) -> str:
    now = _now()
    return jwt.encode({**payload, "iat": now, "exp": now + ttl}, settings.cluster_secret, algorithm=_ALGORITHM)


def decode_cluster_token(token: str, typ: str) -> dict:
    try:
        payload = jwt.decode(token, settings.cluster_secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise ClusterTokenError(str(exc)) from exc
    if payload.get("typ") != typ:
        raise ClusterTokenError(f"se esperaba un token de tipo {typ!r}")
    return payload


# --- tokens de nodo -----------------------------------------------------------
def create_node_token(node_id: str, role: str) -> str:
    return _encode({"typ": "node", "sub": node_id, "role": role}, NODE_TOKEN_TTL)


def node_auth_headers(node_id: str, role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_node_token(node_id, role)}"}


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "invalid_node_token", "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _node_from_header(authorization: str | None, role: str) -> str:
    if not authorization:
        raise _unauthorized("Falta el token de nodo (Authorization: Bearer <token de clúster>)")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("El header Authorization debe ser 'Bearer <token>'")
    try:
        payload = decode_cluster_token(token.strip(), "node")
    except ClusterTokenError as exc:
        raise _unauthorized(f"Token de nodo inválido: {exc}") from exc
    if payload.get("role") != role:
        raise _unauthorized(f"El token no es de un {role}")
    return payload["sub"]


def require_datanode(authorization: str | None = Header(default=None, alias="Authorization")) -> str:
    """Dependencia: exige un token de DataNode y devuelve su node_id."""
    return _node_from_header(authorization, "datanode")


def require_controlnode(authorization: str | None = Header(default=None, alias="Authorization")) -> str:
    """Dependencia: exige un token de ControlNode y devuelve su id."""
    return _node_from_header(authorization, "controlnode")


# --- tokens de bloque ---------------------------------------------------------
def pipeline_entry(node) -> dict:
    """Entrada de la cadena de réplica de un token de escritura: dirección
    INTERNA del DataNode (la que usan los otros nodos)."""
    return {"node_id": node.node_id, "scheme": node.scheme, "host": node.peer_host, "port": node.peer_port}


def create_block_token(
    *,
    block_id: str,
    file_id: int,
    op: str,
    username: str,
    pipeline: list | None = None,
    ttl_seconds: int | None = None,
) -> str:
    payload = {"typ": "block", "bid": block_id, "fid": str(file_id), "op": op, "sub": username}
    if pipeline is not None:
        payload["pipe"] = [pipeline_entry(node) for node in pipeline]
    ttl = timedelta(seconds=ttl_seconds or settings.block_token_ttl_seconds)
    return _encode(payload, ttl)
