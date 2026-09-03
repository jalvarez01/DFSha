"""
Router de gestión del sistema de archivos (RF1): ls / mkdir / rmdir / rm / stat.

Implementa la sección 1 del contrato fijado en CONTRATOS.md. Este módulo es
el dueño del NAMESPACE (el árbol de directorios y los nombres de archivo);
el CONTENIDO de los archivos lo maneja app/routers/transfer.py (RF2).

Toda la resolución de rutas se delega en app/path_service.py: aquí no se
parte, normaliza ni valida ninguna ruta a mano.

Dos puntos de diseño que conviene tener presentes al leer el código:

1. Borrado de contenido ("recolección de blobs")
   Borrar una fila de `files` solo elimina el metadato; el blob y su
   checksum siguen ocupando disco en STORAGE_DIR. Por eso `rm` y
   `rmdir --recursive` recogen los ids de los archivos afectados ANTES de
   borrar y limpian el disco DESPUÉS de que la transacción esté confirmada.
   Ese orden es deliberado: si falla el borrado en disco quedan blobs
   huérfanos (desperdicio de espacio, inofensivo), mientras que al revés
   quedaría metadata apuntando a contenido inexistente (un GET roto).

2. Cascada de borrado
   El subárbol de un `rmdir --recursive` lo borra la cascada del ORM
   (`cascade="all, delete-orphan"` en app/models.py), que carga los hijos y
   recorre el árbol. NO se puede sustituir por un DELETE masivo: el
   `ondelete="CASCADE"` de las claves foráneas no se aplica porque SQLite
   ignora las FK salvo que se active `PRAGMA foreign_keys=ON`, así que un
   borrado masivo dejaría filas huérfanas.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import Directory, File, User
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
    list_children,
    resolve_directory,
    resolve_file,
    resolve_parent_and_name,
    split_parent_and_leaf,
)
# Se importan las FUNCIONES de transfer, nunca la constante STORAGE_DIR: estas
# leen el global del módulo en tiempo de llamada, así que siguen respetando el
# monkeypatch de STORAGE_DIR que hacen los tests. Importar la constante la
# congelaría en el import y los tests borrarían del almacenamiento real.
from app.routers.transfer import _blob_path, _checksum_path
from app.schemas import (
    DeleteResponse,
    DirectoryEntry,
    DirectoryResponse,
    EntryType,
    ErrorResponse,
    ListDirectoryResponse,
    MkdirRequest,
    RmdirRequest,
    RmRequest,
    StatResponse,
)

router = APIRouter()


class DirectoryNotEmptyError(PathError):
    """Se intentó borrar un directorio con contenido sin pedir `recursive`.
    Es propia de este router (no de path_service), porque "solo borro
    directorios vacíos" es una regla de negocio, no de resolución de rutas."""


# Mapa de excepciones -> (status_code, código corto), según la tabla de
# CONTRATOS.md. Se mantiene aquí (y no en un módulo compartido) para que este
# router no dependa del de transferencia más allá del almacenamiento de blobs.
_ERROR_MAP: dict[type[PathError], tuple[int, str]] = {
    InvalidPathError: (400, "invalid_path"),
    NotFoundError: (404, "not_found"),
    NotADirectoryError: (409, "not_a_directory"),
    NotAFileError: (409, "not_a_file"),
    AlreadyExistsError: (409, "already_exists"),
    RootOperationError: (400, "root_operation"),
    DirectoryNotEmptyError: (409, "directory_not_empty"),
}


def _http_error(exc: PathError) -> HTTPException:
    status_code, error_code = _ERROR_MAP.get(type(exc), (400, "path_error"))
    return HTTPException(
        status_code=status_code,
        detail=ErrorResponse(error=error_code, message=str(exc)).model_dump(),
    )


def _join_path(parent_path: str, leaf: str) -> str:
    if parent_path == "/":
        return f"/{leaf}"
    return f"{parent_path}/{leaf}"


def _delete_blobs(file_ids: list[int]) -> None:
    """Borra del disco el contenido de los archivos indicados.

    Los fallos de disco se ignoran a propósito: en ese punto la transacción
    ya está confirmada y el namespace es consistente, así que un unlink
    fallido solo deja un blob huérfano. En Windows es un caso real: una
    descarga concurrente mantiene el archivo abierto y unlink lanza
    PermissionError, que sin capturar convertiría un borrado correcto en un
    error 500.
    """
    for file_id in file_ids:
        blob = _blob_path(file_id)
        for path in (blob, _checksum_path(file_id), blob.with_suffix(".tmp")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue


def _validate_cwd(db: Session, cwd_id: int | None) -> None:
    """Comprueba de una vez que el cwd exista.

    Sin esto, un cwd_id inválido se manifiesta más adelante como un error de
    ruta inválida (400) en vez del 404 que corresponde.
    """
    if cwd_id is not None and db.get(Directory, cwd_id) is None:
        raise NotFoundError(f"El directorio de trabajo (id={cwd_id}) no existe")


def _existing_file_at(db: Session, path: str, cwd_id: int | None) -> File | None:
    """Devuelve el archivo que ocupa exactamente `path`, o None si esa ruta
    no está ocupada por un archivo (o ni siquiera se puede resolver su padre)."""
    try:
        resolved = resolve_parent_and_name(db, path, cwd_id)
    except PathError:
        return None
    return find_child_file(db, resolved.parent.id, resolved.leaf_name)


def _create_child_directory(
    db: Session, parent: Directory, name: str, owner_id: int
) -> Directory:
    """Crea un subdirectorio y lo deja con id asignado (flush), sin confirmar.

    El flush es obligatorio, no una optimización: la sesión de producción usa
    autoflush=False, así que sin él el siguiente nivel de una cadena
    `mkdir -p` leería parent.id == None y, como Directory.parent_id es
    nullable, crearía en silencio una segunda raíz en vez de fallar.

    Se comprueban las dos tablas porque los UniqueConstraint son por tabla:
    a nivel de BD nada impide un archivo y un directorio con el mismo nombre
    en el mismo padre (ver app/models.py).
    """
    if find_child_directory(db, parent.id, name) is not None:
        raise AlreadyExistsError(f"Ya existe un directorio llamado {name!r} en esa ruta")
    if find_child_file(db, parent.id, name) is not None:
        raise AlreadyExistsError(f"Ya existe un archivo llamado {name!r} en esa ruta")

    child = Directory(name=name, parent=parent, owner_id=owner_id)
    db.add(child)
    db.flush()
    return child


def _missing_ancestors(
    db: Session, path: str, cwd_id: int | None
) -> tuple[Directory, list[str]]:
    """Para `mkdir -p`: devuelve el ancestro existente más profundo de `path`
    y los nombres que faltan crear bajo él, ordenados de arriba hacia abajo.

    Es iterativo (y no recursivo) a propósito: una ruta muy profunda haría
    saltar el límite de recursión de Python, y resolver desde la raíz en cada
    nivel de recursión sería cuadrático en la profundidad.
    """
    missing: list[str] = []
    current = path
    while True:
        try:
            return resolve_directory(db, current, cwd_id), list(reversed(missing))
        except NotFoundError:
            # Falta este componente: se apunta y se sigue subiendo. La
            # terminación está garantizada porque split_parent_and_leaf quita
            # un segmento por vuelta y tanto "/" como "." siempre resuelven.
            parent_path, leaf = split_parent_and_leaf(current)
            missing.append(leaf)
            current = parent_path


def _directory_response(db: Session, directory: Directory) -> DirectoryResponse:
    return DirectoryResponse(
        id=directory.id,
        path=get_full_path(db, directory),
        name=directory.name,
        created_at=directory.created_at,
    )


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@router.get("/ls", response_model=ListDirectoryResponse)
def list_directory(
    path: str = "/",
    cwd_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ListDirectoryResponse:
    """Lista el contenido de un directorio.

    Los subdirectorios van antes que los archivos y cada grupo llega ya
    ordenado por nombre desde SQL (list_children); no se reordena en Python
    para no divergir del orden que ven el resto de consumidores.
    """
    try:
        _validate_cwd(db, cwd_id)
        directory = resolve_directory(db, path, cwd_id)
    except PathError as exc:
        raise _http_error(exc) from exc

    subdirs, files = list_children(db, directory)
    entries = [
        DirectoryEntry(
            name=subdir.name,
            type=EntryType.DIRECTORY,
            size_bytes=0,
            created_at=subdir.created_at,
        )
        for subdir in subdirs
    ]
    entries.extend(
        DirectoryEntry(
            name=file_row.name,
            type=EntryType.FILE,
            size_bytes=file_row.size_bytes,
            created_at=file_row.created_at,
        )
        for file_row in files
    )
    return ListDirectoryResponse(path=get_full_path(db, directory), entries=entries)


@router.post("/mkdir", response_model=DirectoryResponse, status_code=201)
def make_directory(
    payload: MkdirRequest,
    response: Response,
    cwd_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DirectoryResponse:
    """Crea un directorio.

    Por defecto el padre debe existir. Con `parents=true` se comporta como
    `mkdir -p`: crea los intermedios que falten y es idempotente si el
    destino ya existe (en ese caso responde 200 en vez de 201).
    """
    try:
        _validate_cwd(db, cwd_id)
        # Rechaza crear la raíz ("/") o el propio cwd (".") antes de mirar
        # `parents`, para que ambos modos den el mismo error en ese caso.
        split_parent_and_leaf(payload.path)
    except PathError as exc:
        raise _http_error(exc) from exc

    try:
        if payload.parents:
            directory = _make_directory_recursive(db, payload.path, cwd_id, user.id, response)
        else:
            resolved = resolve_parent_and_name(db, payload.path, cwd_id)
            directory = _create_child_directory(
                db, resolved.parent, resolved.leaf_name, user.id
            )
        db.commit()
    except PathError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        # Carrera con otra petición que creó el mismo nombre entre la
        # comprobación y el commit: la restricción de unicidad la caza y se
        # traduce al 409 del contrato en vez de escaparse como un 500.
        db.rollback()
        raise _http_error(
            AlreadyExistsError(f"Ya existe una entrada en la ruta {payload.path!r}")
        ) from exc

    db.refresh(directory)
    return _directory_response(db, directory)


def _make_directory_recursive(
    db: Session, path: str, cwd_id: int | None, owner_id: int, response: Response
) -> Directory:
    """Rama `parents=true` de mkdir. Separada para que el endpoint se lea."""
    try:
        existing = resolve_directory(db, path, cwd_id)
    except NotFoundError:
        pass
    except NotADirectoryError as exc:
        # Puede ser que el destino ya esté ocupado por un archivo (mismo caso
        # que el 409 already_exists de la rama no recursiva) o que un
        # componente intermedio lo esté (eso sí es not_a_directory).
        if _existing_file_at(db, path, cwd_id) is not None:
            raise AlreadyExistsError(f"{path} ya existe como archivo") from exc
        raise
    else:
        # Ya existe: mkdir -p es idempotente y no crea nada.
        response.status_code = 200
        return existing

    parent, missing = _missing_ancestors(db, path, cwd_id)
    for name in missing:
        parent = _create_child_directory(db, parent, name, owner_id)
    return parent


@router.delete("/rmdir", response_model=DeleteResponse)
def remove_directory(
    payload: RmdirRequest,
    cwd_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DeleteResponse:
    """Borra un directorio. Sin `recursive` solo borra directorios vacíos."""
    try:
        _validate_cwd(db, cwd_id)
        directory = resolve_directory(db, payload.path, cwd_id)
        if directory.is_root:
            raise RootOperationError("No se puede borrar el directorio raíz '/'")

        subdirs, files = list_children(db, directory)
        if (subdirs or files) and not payload.recursive:
            raise DirectoryNotEmptyError(
                f"El directorio {get_full_path(db, directory)} no está vacío; "
                f"usa recursive=true para borrarlo junto con su contenido"
            )
    except PathError as exc:
        raise _http_error(exc) from exc

    # La ruta y los ids se leen ANTES del borrado: tras el commit el objeto
    # queda desacoplado de la sesión y recorrer sus relaciones fallaría.
    full_path = get_full_path(db, directory)
    file_ids = _collect_descendant_file_ids(db, directory)

    db.delete(directory)
    db.commit()
    _delete_blobs(file_ids)

    return DeleteResponse(path=full_path, deleted=True)


def _collect_descendant_file_ids(db: Session, directory: Directory) -> list[int]:
    """Ids de todos los archivos del subárbol, incluido el nivel actual.

    Devuelve enteros y no objetos ORM a propósito: los objetos quedarían
    desacoplados tras el borrado en cascada y leer su .id ya no sería fiable.
    """
    file_ids: list[int] = []
    pending = [directory]
    while pending:
        current = pending.pop()
        subdirs, files = list_children(db, current)
        file_ids.extend(file_row.id for file_row in files)
        pending.extend(subdirs)
    return file_ids


@router.delete("/rm", response_model=DeleteResponse)
def remove_file(
    payload: RmRequest,
    cwd_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DeleteResponse:
    """Borra un archivo (metadatos y contenido). Para directorios, rmdir."""
    try:
        _validate_cwd(db, cwd_id)
        file_row = resolve_file(db, payload.path, cwd_id)
    except PathError as exc:
        raise _http_error(exc) from exc

    # Igual que en rmdir: capturar id y ruta antes de borrar. resolve_file
    # llega al archivo por un SELECT plano y nunca carga la relación .parent,
    # así que tras el commit ese acceso reventaría.
    file_id = file_row.id
    full_path = _join_path(get_full_path(db, file_row.parent), file_row.name)

    db.delete(file_row)
    db.commit()
    _delete_blobs([file_id])

    return DeleteResponse(path=full_path, deleted=True)


@router.get("/stat", response_model=StatResponse)
def stat_entry(
    path: str = "/",
    cwd_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StatResponse:
    """Metadatos de una entrada cualquiera, sea archivo o directorio.

    Permite a la CLI validar un `cd` sin listar el directorio entero, y
    consultar el tamaño de un archivo sin descargarlo.
    """
    try:
        _validate_cwd(db, cwd_id)
    except PathError as exc:
        raise _http_error(exc) from exc

    directory: Directory | None
    try:
        directory = resolve_directory(db, path, cwd_id)
    except (NotFoundError, NotADirectoryError):
        # No es un directorio (o no existe): puede seguir siendo un archivo.
        # Capturar NotADirectoryError es justo lo que hace que stat funcione
        # sobre archivos; si el path tampoco es un archivo, el error correcto
        # lo produce resolve_file más abajo.
        directory = None
    except PathError as exc:
        raise _http_error(exc) from exc

    if directory is not None:
        return StatResponse(
            path=get_full_path(db, directory),
            # La raíz se guarda con nombre vacío; hacia fuera se muestra "/".
            name=directory.name or "/",
            type=EntryType.DIRECTORY,
            size_bytes=0,
            owner=directory.owner.username,
            created_at=directory.created_at,
            updated_at=None,
        )

    try:
        file_row = resolve_file(db, path, cwd_id)
    except PathError as exc:
        raise _http_error(exc) from exc

    return StatResponse(
        path=_join_path(get_full_path(db, file_row.parent), file_row.name),
        name=file_row.name,
        type=EntryType.FILE,
        size_bytes=file_row.size_bytes,
        owner=file_row.owner.username,
        created_at=file_row.created_at,
        updated_at=file_row.updated_at,
    )
