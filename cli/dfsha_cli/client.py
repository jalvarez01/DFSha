"""
Cliente HTTP hacia la API de DFSha.

Traduce los errores del servidor (forma `ErrorResponse` de
CONTRATOS.md/schemas.py: {"error": "...", "message": "..."}) a
excepciones Python legibles, y distingue un caso particular importante
cuando el servidor no tiene registrada una ruta:

    Un 404 de FastAPI sin ruta registrada trae el cuerpo por defecto
    `{"detail": "Not Found"}` (un string), distinguible del 404 "de
    negocio" que define el contrato
    (`{"detail": {"error": "not_found", "message": "..."}}`, un dict).
    Este cliente levanta `RouteNotImplemented` en el primer caso (p. ej. un
    servidor viejo sin los endpoints del Hito 2) en vez de confundir al
    usuario con un "no encontrado" como si su ruta remota no existiera.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import httpx

from dfsha_cli.auth import Session, build_auth_headers
from dfsha_cli.config import CHUNK_SIZE, DEFAULT_TIMEOUT, UPLOAD_DOWNLOAD_TIMEOUT


class DFShaError(Exception):
    """Base de todos los errores del cliente DFSha."""


@dataclass
class APIError(DFShaError):
    """Error de negocio devuelto por el servidor (forma ErrorResponse)."""

    status_code: int
    error: str
    message: str

    def __str__(self) -> str:
        return f"[{self.status_code} {self.error}] {self.message}"


@dataclass
class RouteNotImplemented(DFShaError):
    """El endpoint todavía no está registrado en el servidor (típicamente
    porque el compañero dueño de ese módulo no ha hecho merge de su
    parte). Distinto de un 404 de negocio (ruta remota inexistente)."""

    method: str
    url: str

    def __str__(self) -> str:
        return (
            f"El servidor respondió 404 'Not Found' genérico para "
            f"{self.method} {self.url}. Probablemente ese endpoint aún "
            f"no ha sido registrado (revisa app/main.py y si el router "
            f"correspondiente ya fue incluido)."
        )


@dataclass
class ConnectionFailed(DFShaError):
    base_url: str
    detail: str

    def __str__(self) -> str:
        return f"No se pudo conectar a {self.base_url}: {self.detail}"


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.is_success:
        return

    method = resp.request.method
    url = str(resp.request.url)

    try:
        body = resp.json()
    except ValueError:
        body = None

    detail = body.get("detail") if isinstance(body, dict) else None

    if resp.status_code == 404 and isinstance(detail, str):
        # 404 por defecto de FastAPI/Starlette: no hay ruta registrada.
        raise RouteNotImplemented(method=method, url=url)

    if isinstance(detail, dict) and "error" in detail and "message" in detail:
        raise APIError(status_code=resp.status_code, error=detail["error"], message=detail["message"])

    # Cualquier otro caso (error 500 inesperado, etc.): igual lo
    # exponemos como APIError con lo que haya, para no perder contexto.
    raise APIError(
        status_code=resp.status_code,
        error="unknown_error",
        message=str(detail) if detail is not None else resp.text[:500],
    )


def _is_not_leader(resp: httpx.Response) -> bool:
    if resp.status_code != 503:
        return False
    try:
        detail = resp.json().get("detail")
    except ValueError:
        return False
    return isinstance(detail, dict) and detail.get("error") == "not_leader"


class DFShaClient:
    """Punto único de acceso HTTP a la API de DFSha. Todas las
    operaciones reciben rutas ya resueltas a absolutas por el llamador
    (ver pathutils.resolve_path); este cliente no conoce el cwd.

    Alta disponibilidad: si la sesión tiene varios ControlNodes, cada
    petición va al último que respondió como líder; si ese no responde o
    contesta 503 `not_leader` (es un respaldo), prueba los demás. Así un
    failover del ControlNode es transparente para el usuario."""

    def __init__(self, session: Session):
        self.session = session
        self.base_urls = session.base_urls
        self.base_url = self.base_urls[0]
        self.verify = session.verify
        self._headers = build_auth_headers(session)

    # -- helpers internos ---------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        timeout = kwargs.pop("timeout", DEFAULT_TIMEOUT)
        headers = {**self._headers, **kwargs.pop("headers", {})}
        candidates = [self.base_url] + [url for url in self.base_urls if url != self.base_url]
        last_error = "ningún ControlNode respondió como líder"
        for base in candidates:
            try:
                resp = httpx.request(
                    method, f"{base}{path}", headers=headers, timeout=timeout, verify=self.verify, **kwargs
                )
            except httpx.HTTPError as exc:
                last_error = str(exc)
                continue
            if _is_not_leader(resp):
                continue
            self.base_url = base
            _raise_for_status(resp)
            return resp
        raise ConnectionFailed(base_url=", ".join(self.base_urls), detail=last_error)

    # -- RF1: gestión del filesystem (contrato /fs/*, Jacobo) ----------
    def ls(self, path: str) -> dict:
        resp = self._request("GET", "/fs/ls", params={"path": path})
        return resp.json()

    def mkdir(self, path: str, parents: bool = False) -> dict:
        resp = self._request("POST", "/fs/mkdir", json={"path": path, "parents": parents})
        return resp.json()

    def stat(self, path: str) -> dict:
        """Metadatos de una entrada (archivo o directorio), sin descargarla."""
        resp = self._request("GET", "/fs/stat", params={"path": path})
        return resp.json()

    def rmdir(self, path: str, recursive: bool = False) -> dict:
        resp = self._request(
            "DELETE", "/fs/rmdir", json={"path": path, "recursive": recursive}
        )
        return resp.json()

    def rm(self, path: str) -> dict:
        resp = self._request("DELETE", "/fs/rm", json={"path": path})
        return resp.json()

    # -- RF2 del Hito 1: transferencia monolítica a través del servidor.
    # La CLI usa ahora la versión distribuida (dfsha_cli/distributed.py);
    # get_file se conserva para archivos subidos con el PUT del Hito 1.
    def put_file(
        self,
        remote_path: str,
        local_path: Path,
        progress_cb: Callable[[int], None] | None = None,
    ) -> dict:
        """Sube `local_path` a `remote_path`, en streaming (nunca carga
        el archivo completo en memoria), tal como espera el servidor
        (body crudo, no multipart)."""

        def _reader() -> Iterator[bytes]:
            with open(local_path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    if progress_cb:
                        progress_cb(len(chunk))
                    yield chunk

        url = f"{self.base_url}/files{remote_path}"
        try:
            resp = httpx.put(
                url,
                content=_reader(),
                headers=self._headers,
                timeout=UPLOAD_DOWNLOAD_TIMEOUT,
                verify=self.verify,
            )
        except httpx.HTTPError as exc:
            raise ConnectionFailed(base_url=self.base_url, detail=str(exc)) from exc
        _raise_for_status(resp)
        return resp.json()

    def get_file(
        self,
        remote_path: str,
        local_path: Path,
        progress_cb: Callable[[int], None] | None = None,
    ) -> dict:
        """Descarga `remote_path` a `local_path`, en streaming. Devuelve
        un dict con metadata útil extraída de los headers de respuesta
        (tamaño, checksum) para que el comando pueda validarla."""
        url = f"{self.base_url}/files{remote_path}"
        try:
            with httpx.stream(
                "GET", url, headers=self._headers, timeout=UPLOAD_DOWNLOAD_TIMEOUT, verify=self.verify
            ) as resp:
                if not resp.is_success:
                    resp.read()
                    _raise_for_status(resp)
                local_path.parent.mkdir(parents=True, exist_ok=True)
                with open(local_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=CHUNK_SIZE):
                        f.write(chunk)
                        if progress_cb:
                            progress_cb(len(chunk))
                checksum = resp.headers.get("X-Checksum-Sha256")
                content_length = resp.headers.get("Content-Length")
        except httpx.HTTPError as exc:
            raise ConnectionFailed(base_url=self.base_url, detail=str(exc)) from exc

        return {
            "checksum_sha256": checksum,
            "size_bytes": int(content_length) if content_length else None,
        }

    # -- Hito 2: ControlNode (plan de bloques y DataNodes) -------------
    @property
    def auth_headers(self) -> dict[str, str]:
        """Headers de autenticación, para propagar el JWT a los DataNodes."""
        return dict(self._headers)

    def allocate(
        self, path: str, size_bytes: int, block_size: int | None = None, lease_id: str | None = None
    ) -> dict:
        """Fase 1 de la escritura: plan de qué bloque va a qué DataNodes."""
        resp = self._request(
            "POST",
            f"/files{path}/allocate",
            json={"size_bytes": size_bytes, "block_size": block_size, "lease_id": lease_id},
        )
        return resp.json()

    def confirm(self, path: str, blocks: list[dict], lease_id: str | None = None) -> dict:
        """Fase 2 de la escritura: checksums y tamaños de lo subido."""
        resp = self._request("POST", f"/files{path}/confirm", json={"blocks": blocks, "lease_id": lease_id})
        return resp.json()

    def blocks(self, path: str) -> dict:
        """Plan de lectura: de qué DataNode bajar cada bloque."""
        resp = self._request("GET", f"/files{path}/blocks")
        return resp.json()

    def datanodes(self) -> list[dict]:
        resp = self._request("GET", "/datanodes")
        return resp.json()

    def cluster_status(self) -> dict:
        return self._request("GET", "/cluster/status").json()

    # -- Hito 3: permisos y grupos -------------------------------------
    def chmod(self, path: str, mode: str) -> dict:
        return self._request("POST", "/fs/chmod", json={"path": path, "mode": mode}).json()

    def chgrp(self, path: str, group: str | None) -> dict:
        return self._request("POST", "/fs/chgrp", json={"path": path, "group": group}).json()

    def get_acl(self, path: str) -> dict:
        return self._request("GET", "/fs/acl", params={"path": path}).json()

    def set_acl(self, path: str, kind: str, name: str, perms: str) -> dict:
        return self._request("POST", "/fs/acl", json={"path": path, "type": kind, "name": name, "perms": perms}).json()

    def remove_acl(self, path: str, kind: str, name: str) -> dict:
        return self._request("DELETE", "/fs/acl", json={"path": path, "type": kind, "name": name}).json()

    def create_group(self, name: str) -> dict:
        return self._request("POST", "/groups", json={"name": name}).json()

    def list_groups(self) -> list[dict]:
        return self._request("GET", "/groups").json()

    def add_group_member(self, group: str, username: str) -> dict:
        return self._request("POST", f"/groups/{group}/members", json={"username": username}).json()

    def remove_group_member(self, group: str, username: str) -> dict:
        return self._request("DELETE", f"/groups/{group}/members/{username}").json()

    def delete_group(self, group: str) -> None:
        self._request("DELETE", f"/groups/{group}")

    # -- Hito 3: locks -------------------------------------------------
    def lock(self, path: str, mode: str = "exclusive", ttl_seconds: int | None = None) -> dict:
        return self._request("POST", f"/files{path}/lock", json={"mode": mode, "ttl_seconds": ttl_seconds}).json()

    def renew_lock(self, path: str, lock_id: str, ttl_seconds: int | None = None) -> dict:
        return self._request("POST", f"/files{path}/lock/{lock_id}/renew", json={"ttl_seconds": ttl_seconds}).json()

    def unlock(self, path: str, lock_id: str) -> dict:
        return self._request("DELETE", f"/files{path}/lock/{lock_id}").json()

    def locks(self, path: str) -> list[dict]:
        return self._request("GET", f"/files{path}/locks").json()

    # -- Hito 3: 2FA y API keys ----------------------------------------
    def two_factor_status(self) -> dict:
        return self._request("GET", "/auth/2fa").json()

    def two_factor_setup(self) -> dict:
        return self._request("POST", "/auth/2fa/setup").json()

    def two_factor_enable(self, code: str) -> dict:
        return self._request("POST", "/auth/2fa/enable", json={"code": code}).json()

    def two_factor_disable(self, code: str) -> dict:
        return self._request("POST", "/auth/2fa/disable", json={"code": code}).json()

    def create_api_key(self, name: str, expires_in_days: int | None = None) -> dict:
        return self._request("POST", "/auth/api-keys", json={"name": name, "expires_in_days": expires_in_days}).json()

    def list_api_keys(self) -> list[dict]:
        return self._request("GET", "/auth/api-keys").json()

    def revoke_api_key(self, key_id: int) -> dict:
        return self._request("DELETE", f"/auth/api-keys/{key_id}").json()

    def health(self) -> dict:
        resp = self._request("GET", "/health")
        return resp.json()
