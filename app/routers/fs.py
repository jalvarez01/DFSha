"""
Router de gestión del sistema de archivos (RF1): ls / mkdir / rmdir / rm / stat.

Implementa la sección 1 del contrato fijado en CONTRATOS.md. Este módulo es
el dueño del NAMESPACE (el árbol de directorios y los nombres de archivo);
el CONTENIDO de los archivos lo maneja app/routers/transfer.py (RF2).

Toda la resolución de rutas se delega en app/path_service.py: aquí no se
parte, normaliza ni valida ninguna ruta a mano.

Dos puntos de diseño que conviene tener presentes al leer el código:

1. Borrado de contenido ("recolección de blobs y bloques")
   Borrar una fila de `files` solo elimina el metadato; el contenido sigue
   ocupando disco. Por eso `rm` y `rmdir --recursive` recogen los ids de los
   archivos afectados ANTES de borrar y:
   - Hito 2 (distribuido): en la misma transacción encolan el borrado de
     sus bloques en los DataNodes (app/block_gc.py); cada DataNode recibe
     la orden en su próximo heartbeat.
   - Hito 1 (monolítico): limpian el blob local DESPUÉS del commit. Si falla
     el borrado en disco quedan blobs huérfanos (inofensivo), mientras que
     al revés quedaría metadata apuntando a contenido inexistente.

2. Control de acceso (Hito 3)
   Cada endpoint comprueba permisos con app/acl.py antes de tocar nada:
   atravesar exige `x` en los ancestros, listar `r`, crear/borrar `w+x` en
   el padre (con sticky bit, solo el dueño borra). chmod, chgrp y ACL están
   aquí también: son parte de la gestión del sistema de archivos.

3. Cascada de borrado
   El subárbol de un `rmdir --recursive` lo borra la cascada del ORM
   (`cascade="all, delete-orphan"` en app/models.py), que carga los hijos y
   recorre el árbol. NO se puede sustituir por un DELETE masivo: el
   `ondelete="CASCADE"` de las claves foráneas no se aplica porque SQLite
   ignora las FK salvo que se active `PRAGMA foreign_keys=ON`, así que un
   borrado masivo dejaría filas huérfanas.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import acl
from app.block_gc import discard_blocks_of_files
from app.database import get_db
from app.deps import get_current_user
from app.locks import assert_not_locked_by_others
from app.models import AclEntry, Directory, File, Group, GroupMember, User
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
# Se importa la FUNCIÓN de transfer, nunca la constante STORAGE_DIR: la
# función lee el global del módulo en tiempo de llamada, así que sigue
# respetando el monkeypatch de STORAGE_DIR que hacen los tests. Importar la
# constante la congelaría en el import y los tests borrarían del
# almacenamiento real.
from app.routers.transfer import delete_local_blobs
from app.schemas import (
    AclEntryModel,
    AclRemoveRequest,
    AclResponse,
    AclSetRequest,
    ChgrpRequest,
    ChmodRequest,
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
    db: Session, parent: Directory, name: str, user: User
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
    acl.require_create(db, user, parent)

    child = Directory(name=name, parent=parent, owner_id=user.id)
    acl.apply_creation_defaults(child, parent)
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


def _permission_fields(node: Directory | File) -> dict:
    return {
        "owner": node.owner.username,
        "group": node.group.name if node.group else None,
        "mode": acl.mode_string(node),
    }


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
    if directory.parent is not None:
        acl.require_traverse(db, user, directory.parent)
    acl.require(db, user, directory, acl.R, "no puedes listar el directorio")

    subdirs, files = list_children(db, directory)
    entries = [
        DirectoryEntry(
            name=subdir.name,
            type=EntryType.DIRECTORY,
            size_bytes=0,
            created_at=subdir.created_at,
            **_permission_fields(subdir),
        )
        for subdir in subdirs
    ]
    entries.extend(
        DirectoryEntry(
            name=file_row.name,
            type=EntryType.FILE,
            size_bytes=file_row.size_bytes,
            created_at=file_row.created_at,
            **_permission_fields(file_row),
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
            directory = _make_directory_recursive(db, payload.path, cwd_id, user, response)
        else:
            resolved = resolve_parent_and_name(db, payload.path, cwd_id)
            directory = _create_child_directory(
                db, resolved.parent, resolved.leaf_name, user
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
    db: Session, path: str, cwd_id: int | None, user: User, response: Response
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
        parent = _create_child_directory(db, parent, name, user)
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
    acl.require_delete(db, user, directory.parent, directory)
    if payload.recursive:
        _check_recursive_delete(db, user, directory)

    # La ruta y los ids se leen ANTES del borrado: tras el commit el objeto
    # queda desacoplado de la sesión y recorrer sus relaciones fallaría.
    full_path = get_full_path(db, directory)
    file_ids = _collect_descendant_file_ids(db, directory)

    discard_blocks_of_files(db, file_ids)
    db.delete(directory)
    db.commit()
    delete_local_blobs(file_ids)

    return DeleteResponse(path=full_path, deleted=True)


def _check_recursive_delete(db: Session, user: User, directory: Directory) -> None:
    """`rm -r`: hace falta poder vaciar cada directorio del subárbol (rwx en
    él, y el sticky bit respetado) y que ningún archivo esté bloqueado por
    otro usuario. Se comprueba todo ANTES de borrar nada."""
    pending = [directory]
    while pending:
        current = pending.pop()
        subdirs, files = list_children(db, current)
        if subdirs or files:
            acl.require(db, user, current, acl.R | acl.W | acl.X, "no puedes vaciar el directorio")
        for child in [*subdirs, *files]:
            if current.mode & acl.STICKY and not acl.is_superuser(user):
                if user.id not in (child.owner_id, current.owner_id):
                    raise acl.PermissionDenied(
                        f"Permiso denegado: {child.name} está en un directorio con sticky bit y no es tuyo"
                    )
        for file_row in files:
            assert_not_locked_by_others(db, file_row, user)
        pending.extend(subdirs)


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
    acl.require_delete(db, user, file_row.parent, file_row)
    assert_not_locked_by_others(db, file_row, user)

    # Igual que en rmdir: capturar id y ruta antes de borrar. resolve_file
    # llega al archivo por un SELECT plano y nunca carga la relación .parent,
    # así que tras el commit ese acceso reventaría.
    file_id = file_row.id
    full_path = _join_path(get_full_path(db, file_row.parent), file_row.name)

    discard_blocks_of_files(db, [file_id])
    db.delete(file_row)
    db.commit()
    delete_local_blobs([file_id])

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
        if directory.parent is not None:
            acl.require_traverse(db, user, directory.parent)
        return StatResponse(
            path=get_full_path(db, directory),
            # La raíz se guarda con nombre vacío; hacia fuera se muestra "/".
            name=directory.name or "/",
            type=EntryType.DIRECTORY,
            size_bytes=0,
            owner=directory.owner.username,
            created_at=directory.created_at,
            updated_at=None,
            group=directory.group.name if directory.group else None,
            mode=acl.mode_string(directory),
            mode_octal=f"{directory.mode:04o}",
        )

    try:
        file_row = resolve_file(db, path, cwd_id)
    except PathError as exc:
        raise _http_error(exc) from exc
    acl.require_traverse(db, user, file_row.parent)

    return StatResponse(
        path=_join_path(get_full_path(db, file_row.parent), file_row.name),
        name=file_row.name,
        type=EntryType.FILE,
        size_bytes=file_row.size_bytes,
        owner=file_row.owner.username,
        created_at=file_row.created_at,
        updated_at=file_row.updated_at,
        group=file_row.group.name if file_row.group else None,
        mode=acl.mode_string(file_row),
        mode_octal=f"{file_row.mode:04o}",
    )


# --------------------------------------------------------------------------
# Permisos: chmod, chgrp y ACL (Hito 3)
# --------------------------------------------------------------------------
def _resolve_node(db: Session, path: str) -> tuple[Directory | File, str]:
    """Resuelve una ruta a directorio o archivo, comprobando que se pueda
    llegar a ella (x en los ancestros). Devuelve también su ruta absoluta."""
    try:
        directory = resolve_directory(db, path)
    except (NotFoundError, NotADirectoryError):
        directory = None
    except PathError as exc:
        raise _http_error(exc) from exc
    if directory is not None:
        return directory, get_full_path(db, directory)
    try:
        file_row = resolve_file(db, path)
    except PathError as exc:
        raise _http_error(exc) from exc
    return file_row, _join_path(get_full_path(db, file_row.parent), file_row.name)


def _acl_response(db: Session, node: Directory | File, path: str) -> AclResponse:
    entries = []
    for entry in node.acl_entries:
        if entry.user_id is not None:
            entries.append(AclEntryModel(type="user", name=entry.user.username, perms=acl.perms_string(entry.perms)))
        else:
            entries.append(AclEntryModel(type="group", name=entry.group.name, perms=acl.perms_string(entry.perms)))
    return AclResponse(
        path=path,
        owner=node.owner.username,
        group=node.group.name if node.group else None,
        mode=acl.mode_string(node),
        mode_octal=f"{node.mode:04o}",
        entries=sorted(entries, key=lambda e: (e.type, e.name)),
    )


def _bad_request(error: str, message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=ErrorResponse(error=error, message=message).model_dump())


def _principal(db: Session, kind: str, name: str) -> User | Group:
    model = User if kind == "user" else Group
    column = User.username if kind == "user" else Group.name
    row = db.scalar(select(model).where(column == name))
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=ErrorResponse(error="not_found", message=f"No existe el {'usuario' if kind == 'user' else 'grupo'} {name!r}").model_dump(),
        )
    return row


def _guard_owner(db: Session, user: User, node: Directory | File) -> None:
    parent = node.parent
    if parent is not None:
        acl.require_traverse(db, user, parent)
    acl.require_owner(user, node)


@router.post("/chmod", response_model=AclResponse)
def change_mode(payload: ChmodRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Cambia los bits de modo (octal, como chmod 750). Solo el dueño."""
    node, path = _resolve_node(db, payload.path)
    _guard_owner(db, user, node)
    try:
        mode = int(payload.mode, 8)
    except ValueError:
        raise _bad_request("invalid_mode", f"Modo inválido: {payload.mode!r} (usa octal, p. ej. 750)") from None
    if not 0 <= mode <= 0o1777:
        raise _bad_request("invalid_mode", f"Modo fuera de rango: {payload.mode!r}")
    node.mode = mode
    db.commit()
    db.refresh(node)
    return _acl_response(db, node, path)


@router.post("/chgrp", response_model=AclResponse)
def change_group(payload: ChgrpRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Cambia el grupo dueño. Solo el dueño, y solo a un grupo del que sea miembro."""
    node, path = _resolve_node(db, payload.path)
    _guard_owner(db, user, node)
    if payload.group is None:
        node.group_id = None
    else:
        group = _principal(db, "group", payload.group)
        is_member = db.scalar(
            select(GroupMember.id).where(GroupMember.group_id == group.id, GroupMember.user_id == user.id)
        )
        if is_member is None and not acl.is_superuser(user):
            raise acl.PermissionDenied(f"Permiso denegado: no eres miembro del grupo {group.name!r}")
        node.group_id = group.id
    db.commit()
    db.refresh(node)
    return _acl_response(db, node, path)


@router.get("/acl", response_model=AclResponse)
def get_acl(path: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    node, full_path = _resolve_node(db, path)
    if node.parent is not None:
        acl.require_traverse(db, user, node.parent)
    return _acl_response(db, node, full_path)


@router.post("/acl", response_model=AclResponse)
def set_acl(payload: AclSetRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Añade o cambia una entrada de ACL (setfacl -m). Solo el dueño."""
    node, path = _resolve_node(db, payload.path)
    _guard_owner(db, user, node)
    try:
        perms = acl.parse_perms(payload.perms)
    except ValueError as exc:
        raise _bad_request("invalid_perms", str(exc)) from exc
    principal = _principal(db, payload.type, payload.name)
    for entry in node.acl_entries:
        if (payload.type == "user" and entry.user_id == principal.id) or (
            payload.type == "group" and entry.group_id == principal.id
        ):
            entry.perms = perms
            break
    else:
        entry = AclEntry(perms=perms)
        if payload.type == "user":
            entry.user_id = principal.id
        else:
            entry.group_id = principal.id
        node.acl_entries.append(entry)
    db.commit()
    db.refresh(node)
    return _acl_response(db, node, path)


@router.delete("/acl", response_model=AclResponse)
def remove_acl(payload: AclRemoveRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Quita una entrada de ACL (setfacl -x). Solo el dueño."""
    node, path = _resolve_node(db, payload.path)
    _guard_owner(db, user, node)
    principal = _principal(db, payload.type, payload.name)
    for entry in list(node.acl_entries):
        if (payload.type == "user" and entry.user_id == principal.id) or (
            payload.type == "group" and entry.group_id == principal.id
        ):
            node.acl_entries.remove(entry)
    db.commit()
    db.refresh(node)
    return _acl_response(db, node, path)
