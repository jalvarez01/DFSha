"""
Cliente HTTP hacia la API de DFSha.

Traduce los errores del servidor (forma `ErrorResponse` de
CONTRATOS.md/schemas.py: {"error": "...", "message": "..."}) a
excepciones Python legibles, y distingue un caso particular importante
en el estado actual del proyecto:

    Los endpoints /fs/* (ls, mkdir, rmdir, rm) todavía NO están
    registrados en app/main.py (el compañero de FS no ha terminado su
    parte). Si se llama a uno de esos endpoints hoy, FastAPI responde
    404 con el cuerpo por defecto `{"detail": "Not Found"}` (un string),
    que es distinguible del 404 "de negocio" que sí define el contrato
    (`{"detail": {"error": "not_found", "message": "..."}}`, un dict).

    Este cliente detecta esa diferencia y levanta `RouteNotImplemented`
    en el primer caso, con un mensaje claro de "esto todavía no existe
    en el servidor", en vez de confundir al usuario con un genérico
    "no encontrado" como si su ruta remota no existiera.
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


class DFShaClient:
    """Punto único de acceso HTTP a la API de DFSha. Todas las
    operaciones reciben rutas ya resueltas a absolutas por el llamador
    (ver pathutils.resolve_path); este cliente no conoce el cwd."""

    def __init__(self, session: Session):
        self.session = session
        self.base_url = session.base_url.rstrip("/")
        self._headers = build_auth_headers(session)

    # -- helpers internos ---------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = f"{self.base_url}{path}"
        timeout = kwargs.pop("timeout", DEFAULT_TIMEOUT)
        headers = {**self._headers, **kwargs.pop("headers", {})}
        try:
            resp = httpx.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except httpx.HTTPError as exc:
            raise ConnectionFailed(base_url=self.base_url, detail=str(exc)) from exc
        _raise_for_status(resp)
        return resp

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

    # -- RF2: transferencia (contrato /files/*, Paulina) ---------------
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
                "GET", url, headers=self._headers, timeout=UPLOAD_DOWNLOAD_TIMEOUT
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

    def health(self) -> dict:
        resp = self._request("GET", "/health")
        return resp.json()
