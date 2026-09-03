"""
Router de transferencia de archivos (RF2): PUT y GET.


1. Guardar/leer el CONTENIDO real de cada archivo en disco (fuera de la
   base de datos — SQLite no es el lugar para blobs grandes).
2. Mantener `File.size_bytes` sincronizado con lo realmente almacenado.
3. Traducir las excepciones de `path_service` a los códigos HTTP de la
   tabla del contrato.

Diseño del almacenamiento en disco
-----------------------------------
Cada archivo se guarda como `<STORAGE_DIR>/<file.id>`, es decir, indexado
por el id autoincremental de la fila en la tabla `files`, NO por su
nombre o ruta. Esto evita cualquier ambigüedad si el archivo se mueve o
renombra en el futuro (RF1), y elimina problemas de caracteres inválidos
en nombres de archivo del sistema operativo anfitrión.

Junto a cada blob se guarda un archivo `<file.id>.sha256` con el
checksum SHA-256 del contenido, calculado en streaming mientras se
recibe el PUT (sin cargar el archivo completo en memoria). Esto sirve
para:
- Verificar integridad al leer (GET) sin tener que re-descargar.
- Exponerlo en el header `X-Checksum-Sha256` para que el cliente lo
  compare tras la descarga (igual patrón que RF3 usará más adelante).

La subida y bajada se hacen en STREAMING (chunks de 64KB) en ambas
direcciones: el body crudo de la request en PUT, y una StreamingResponse
en GET. Así nunca se carga un archivo grande completo en memoria del
servidor (alineado con RNF1: escalabilidad en tamaño de archivos).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import File as FileModel
from app.models import User
from app.path_service import (
    AlreadyExistsError,
    InvalidPathError,
    NotADirectoryError,
    NotAFileError,
    NotFoundError,
    PathError,
    RootOperationError,
    find_child_directory,
    find_child_file,
    get_full_path,
    resolve_file,
    resolve_parent_and_name,
)
from app.schemas import ErrorResponse, FileMetadata, PutFileResponse

router = APIRouter()

# Directorio donde se guarda el CONTENIDO de los archivos (separado de la
# BD, que solo tiene metadatos). Configurable por env var para que en
# Docker apunte al volumen persistente /data (ver Dockerfile: VOLUME
# ["/data"]), igual que DATABASE_URL.
STORAGE_DIR = Path(os.getenv("DFSHA_STORAGE_DIR", "./data/blocks"))

CHUNK_SIZE = 64 * 1024  # 64KB, igual criterio que el resto del proyecto

# Mapa de excepciones de path_service -> (status_code, código corto de error)
# según la tabla de CONTRATOS.md.
_ERROR_MAP: dict[type[PathError], tuple[int, str]] = {
    InvalidPathError: (400, "invalid_path"),
    NotFoundError: (404, "not_found"),
    NotADirectoryError: (409, "not_a_directory"),
    NotAFileError: (409, "not_a_file"),
    AlreadyExistsError: (409, "already_exists"),
    RootOperationError: (400, "root_operation"),
}


def _http_error(exc: PathError) -> HTTPException:
    status_code, error_code = _ERROR_MAP.get(type(exc), (400, "path_error"))
    return HTTPException(
        status_code=status_code,
        detail=ErrorResponse(error=error_code, message=str(exc)).model_dump(),
    )


def _blob_path(file_id: int) -> Path:
    return STORAGE_DIR / str(file_id)


def _checksum_path(file_id: int) -> Path:
    return STORAGE_DIR / f"{file_id}.sha256"


def _read_checksum(file_id: int) -> str | None:
    path = _checksum_path(file_id)
    if not path.exists():
        return None
    return path.read_text().strip()


def _as_absolute(path: str) -> str:
   
    return path if path.startswith("/") else f"/{path}"


def _join_path(parent_path: str, leaf: str) -> str:
   
    if parent_path == "/":
        return f"/{leaf}"
    return f"{parent_path}/{leaf}"


def _to_metadata(db: Session, file_row: FileModel) -> FileMetadata:
    parent_full_path = get_full_path(db, file_row.parent)
    return FileMetadata(
        id=file_row.id,
        path=_join_path(parent_full_path, file_row.name),
        name=file_row.name,
        size_bytes=file_row.size_bytes,
        owner=file_row.owner.username,
        created_at=file_row.created_at,
        updated_at=file_row.updated_at,
    )


@router.put("/{path:path}", response_model=PutFileResponse)
async def put_file(
    path: str,
    request: Request,
    cwd_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PutFileResponse:
    
    path = _as_absolute(path)
    try:
        resolved = resolve_parent_and_name(db, path, cwd_id)
    except PathError as exc:
        raise _http_error(exc) from exc

    parent = resolved.parent
    leaf_name = resolved.leaf_name

    # Si ya existe un DIRECTORIO con ese nombre en el mismo padre, un PUT
    # no puede "sobreescribirlo" como archivo.
    if find_child_directory(db, parent.id, leaf_name) is not None:
        raise _http_error(
            NotADirectoryError(f"{path} ya existe como directorio, no se puede sobreescribir con un archivo")
        )

    existing = find_child_file(db, parent.id, leaf_name)
    created = existing is None

    STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    if existing is not None:
        file_row = existing
    else:
        file_row = FileModel(name=leaf_name, parent_id=parent.id, owner_id=user.id, size_bytes=0)
        db.add(file_row)
        db.flush()  # asigna file_row.id sin comprometer la transacción todavía

    blob_path = _blob_path(file_row.id)
    tmp_path = blob_path.with_suffix(".tmp")

    hasher = hashlib.sha256()
    size = 0
    try:
        with open(tmp_path, "wb") as f:
            async for chunk in request.stream():
                if not chunk:
                    continue
                f.write(chunk)
                hasher.update(chunk)
                size += len(chunk)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        db.rollback()
        raise

    # Solo se reemplaza el blob real una vez que la escritura completa
    # tuvo éxito: si algo falla a mitad de camino, el archivo anterior
    # (en un update) queda intacto.
    tmp_path.replace(blob_path)
    _checksum_path(file_row.id).write_text(hasher.hexdigest())

    file_row.size_bytes = size
    db.commit()
    db.refresh(file_row)

    return PutFileResponse(file=_to_metadata(db, file_row), created=created)


@router.get("/{path:path}")
async def get_file(
    path: str,
    cwd_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Descarga un archivo, en streaming (no carga el contenido completo
    en memoria del servidor)."""
    path = _as_absolute(path)
    try:
        file_row = resolve_file(db, path, cwd_id)
    except PathError as exc:
        raise _http_error(exc) from exc

    blob_path = _blob_path(file_row.id)
    if not blob_path.exists():
        # Caso de inconsistencia: hay metadata en la BD pero no contenido
        # en disco (no debería pasar en operación normal).
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="storage_missing",
                message="El archivo existe en el índice pero no se encontró su contenido almacenado",
            ).model_dump(),
        )

    def iter_blob():
        with open(blob_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    headers = {
        "Content-Disposition": f'attachment; filename="{file_row.name}"',
        "Content-Length": str(file_row.size_bytes),
    }
    checksum = _read_checksum(file_row.id)
    if checksum:
        headers["X-Checksum-Sha256"] = checksum

    return StreamingResponse(iter_blob(), media_type="application/octet-stream", headers=headers)


@router.head("/{path:path}")
def head_file(
    path: str,
    cwd_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    
    path = _as_absolute(path)
    try:
        file_row = resolve_file(db, path, cwd_id)
    except PathError as exc:
        raise _http_error(exc) from exc

    headers = {"Content-Length": str(file_row.size_bytes)}
    checksum = _read_checksum(file_row.id)
    if checksum:
        headers["X-Checksum-Sha256"] = checksum
    return Response(headers=headers)
