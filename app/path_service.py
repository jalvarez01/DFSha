"""
Servicio de resolución y validación de rutas jerárquicas (tipo Linux).

Este módulo es la pieza central que MIS COMPAÑEROS deben usar desde sus
endpoints en vez de reimplementar lógica de rutas cada uno por su lado:

- Compañero de FS (ls/mkdir/rmdir/rm): usa `resolve_directory`,
  `resolve_parent_and_name`, `list_children`, `get_full_path`.
- Compañero de transferencia (put/get): usa `resolve_parent_and_name`
  (para put, crear el File dentro del directorio padre) y
  `resolve_file` (para get, ubicar el File a partir de la ruta).
- Compañero de CLI: no llama a este módulo directamente (llama a la API
  HTTP), pero puede leer este archivo para entender qué errores puede
  recibir de la API (ver la sección "Errores" más abajo y CONTRACTS.md).

Diseño en dos capas:

1. Funciones PURAS de string (no tocan la BD): `normalize_path`,
   `split_segments`, `validate_segment_name`, `split_parent_and_leaf`.
   Son las que se prueban exhaustivamente en tests/test_path_service.py
   sin necesidad de una base de datos.

2. Funciones de RESOLUCIÓN (sí tocan la BD, reciben una `Session`):
   `resolve_directory`, `resolve_file`, `resolve_parent_and_name`,
   `list_children`, `get_full_path`, `get_or_create_root`.
   Son "puras" en el sentido de negocio: no dependen de FastAPI ni de
   HTTP, solo de SQLAlchemy, así que se pueden llamar desde cualquier
   endpoint o testear con una BD SQLite en memoria.

Convención de rutas:
- Absolutas: empiezan con "/", se resuelven siempre desde la raíz.
- Relativas: no empiezan con "/", se resuelven a partir de un
  `cwd_id` (id de un Directory) que el llamador debe pasar.
- "." significa "el propio directorio" y ".." "el directorio padre",
  igual que en Linux. ".." en la raíz absoluta se queda en la raíz
  (no es un error, tal como en Linux con "/..").
- Se colapsan slashes repetidos ("//") y se ignoran segmentos vacíos.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Directory, File

# Nombre reservado para la raíz del árbol (no es un nombre "de usuario",
# solo se usa internamente al construir rutas legibles).
ROOT_NAME = ""

# Un segmento de ruta válido: no vacío, sin '/', sin caracteres de control,
# y explícitamente sin ser "." o ".." (esos se manejan aparte como
# navegación, nunca como nombres reales de archivo/directorio).
_INVALID_NAME_CHARS = re.compile(r"[\x00-\x1f]")
_MAX_NAME_LENGTH = 255


# --------------------------------------------------------------------------
# Errores
# --------------------------------------------------------------------------
class PathError(Exception):
    """Excepción base de todos los errores de rutas. Los compañeros de FS
    y transferencia deben capturar (al menos) esta clase en sus endpoints
    y traducirla al código HTTP que corresponda (ver CONTRACTS.md)."""


class InvalidPathError(PathError):
    """La ruta o alguno de sus segmentos es sintácticamente inválido
    (vacío, con caracteres prohibidos, demasiado largo, etc). Sugerido: 400."""


class NotFoundError(PathError):
    """Algún componente de la ruta no existe. Sugerido: 404."""


class NotADirectoryError(PathError):
    """Se esperaba un directorio pero la ruta apunta a un archivo, o
    viceversa (NotAFileError). Sugerido: 400/409."""


class NotAFileError(PathError):
    """Se esperaba un archivo pero la ruta apunta a un directorio. Sugerido: 400/409."""


class AlreadyExistsError(PathError):
    """Ya existe un archivo o directorio con ese nombre en el padre
    indicado. Sugerido: 409."""


class RootOperationError(PathError):
    """Operación no permitida sobre la raíz (p. ej. borrarla o renombrarla).
    Sugerido: 400."""


# --------------------------------------------------------------------------
# Capa 1: funciones puras de string (sin BD)
# --------------------------------------------------------------------------
def validate_segment_name(name: str) -> None:
    """Valida un único segmento de ruta (un nombre de archivo o directorio).

    Lanza InvalidPathError si el nombre está vacío, es "." o "..", contiene
    "/" o caracteres de control, o excede la longitud máxima permitida.
    """
    if not name:
        raise InvalidPathError("El nombre no puede estar vacío")
    if name in (".", ".."):
        raise InvalidPathError(f"{name!r} no es un nombre válido (es un segmento de navegación)")
    if "/" in name:
        raise InvalidPathError(f"El nombre {name!r} no puede contener '/'")
    if _INVALID_NAME_CHARS.search(name):
        raise InvalidPathError(f"El nombre {name!r} contiene caracteres no permitidos")
    if len(name) > _MAX_NAME_LENGTH:
        raise InvalidPathError(
            f"El nombre {name!r} excede el máximo de {_MAX_NAME_LENGTH} caracteres"
        )


def split_segments(path: str) -> list[str]:
    """Divide una ruta cruda en sus segmentos, ignorando slashes repetidos
    y segmentos vacíos producidos por ellos. No resuelve '.' ni '..'.

        "/a//b/"   -> ["a", "b"]
        "a/./b"    -> ["a", ".", "b"]
        ""         -> []
    """
    return [seg for seg in path.split("/") if seg != ""]


def normalize_path(path: str) -> str:
    """Normaliza una ruta colapsando '.' y '..' a nivel puramente textual.

    - Rutas absolutas (empiezan con "/"): siempre se puede resolver "..",
      ya que se sabe que se parte de la raíz. Un ".." que se topa con la
      raíz simplemente se queda en la raíz (igual que en Linux).
    - Rutas relativas: un ".." que sobrepasa lo que ya se acumuló en la
      ruta (p. ej. la ruta relativa empieza con "..") no se puede colapsar
      sin conocer el directorio de trabajo, así que se conserva tal cual
      al inicio del resultado; será `resolve_directory` quien lo aplique
      contra el cwd real.

    Lanza InvalidPathError si la ruta es vacía o algún segmento (que no
    sea "." o "..") es inválido según `validate_segment_name`.
    """
    if path == "":
        raise InvalidPathError("La ruta no puede estar vacía")

    is_absolute = path.startswith("/")
    stack: list[str] = []

    for seg in split_segments(path):
        if seg == ".":
            continue
        if seg == "..":
            if stack and stack[-1] != "..":
                stack.pop()
            elif is_absolute:
                # ".." en la raíz absoluta: no-op, como en Linux.
                continue
            else:
                # Ruta relativa que sube por encima de su propio inicio:
                # se conserva el ".." para que la resolución con cwd lo
                # aplique más adelante.
                stack.append("..")
            continue
        validate_segment_name(seg)
        stack.append(seg)

    normalized = "/".join(stack)
    if is_absolute:
        return "/" + normalized
    return normalized if normalized else "."


def split_parent_and_leaf(path: str) -> tuple[str, str]:
    """Separa una ruta normalizada en (ruta_del_padre, nombre_hoja).

    Útil para operaciones de creación (mkdir, put de un archivo): primero
    se resuelve el padre (que debe existir) y luego se valida/crea la hoja.

        "/a/b/c" -> ("/a/b", "c")
        "a/b/c"  -> ("a/b", "c")
        "c"      -> (".", "c")
        "/c"     -> ("/", "c")

    Lanza InvalidPathError si la ruta apunta a la raíz misma (no tiene
    "hoja" que crear/borrar) o si termina en "..".
    """
    normalized = normalize_path(path)
    is_absolute = normalized.startswith("/")
    segments = [] if normalized == "." else split_segments(normalized)

    if not segments:
        if is_absolute:
            raise RootOperationError(
                "La ruta apunta a la raíz; no tiene un nombre para esta operación"
            )
        raise InvalidPathError(
            "La ruta equivale al propio directorio de trabajo; no tiene un nombre final"
        )
    if segments[-1] == "..":
        raise InvalidPathError("La ruta no puede terminar en '..' para esta operación")

    leaf = segments[-1]
    parent_segments = segments[:-1]
    if is_absolute:
        parent_path = "/" + "/".join(parent_segments)
    else:
        parent_path = "/".join(parent_segments) if parent_segments else "."
    return parent_path, leaf


# --------------------------------------------------------------------------
# Capa 2: resolución contra la base de datos
# --------------------------------------------------------------------------
def get_or_create_root(db: Session, owner_id: int) -> Directory:
    """Devuelve el directorio raíz ("/"), creándolo si es la primera vez
    que arranca el servidor. Se llama típicamente una sola vez, en el
    evento de startup de FastAPI (ver app/main.py)."""
    root = db.scalar(select(Directory).where(Directory.parent_id.is_(None)))
    if root is not None:
        return root
    root = Directory(name=ROOT_NAME, parent_id=None, owner_id=owner_id)
    db.add(root)
    db.commit()
    db.refresh(root)
    return root


def get_root_directory(db: Session) -> Directory:
    """Obtiene la raíz existente. Lanza si aún no fue inicializada (no
    debería pasar en un servidor ya arrancado; ver `get_or_create_root`)."""
    root = db.scalar(select(Directory).where(Directory.parent_id.is_(None)))
    if root is None:
        raise NotFoundError("El directorio raíz no ha sido inicializado")
    return root


def find_child_directory(db: Session, parent_id: int, name: str) -> Directory | None:
    return db.scalar(
        select(Directory).where(Directory.parent_id == parent_id, Directory.name == name)
    )


def find_child_file(db: Session, parent_id: int, name: str) -> File | None:
    return db.scalar(select(File).where(File.parent_id == parent_id, File.name == name))


def get_full_path(db: Session, directory: Directory) -> str:
    """Reconstruye la ruta absoluta ("/a/b/c") de un Directory, caminando
    hacia arriba por sus padres hasta la raíz."""
    names: list[str] = []
    current: Directory | None = directory
    while current is not None and current.parent_id is not None:
        names.append(current.name)
        current = current.parent
    return "/" + "/".join(reversed(names))


def _resolve_segments(db: Session, path: str, cwd_id: int | None) -> list[str]:
    """Normaliza `path` y lo convierte en la lista de segmentos absolutos
    finales (desde la raíz), resolviendo '..' relativos contra el cwd si
    hace falta. No toca las tablas de directorios/archivos todavía."""
    normalized = normalize_path(path)

    if normalized.startswith("/"):
        return split_segments(normalized)

    if cwd_id is None:
        raise InvalidPathError(
            "La ruta es relativa pero no se indicó un directorio de trabajo (cwd_id)"
        )
    cwd_dir = db.get(Directory, cwd_id)
    if cwd_dir is None:
        raise NotFoundError(f"El directorio de trabajo (id={cwd_id}) no existe")

    base = split_segments(get_full_path(db, cwd_dir))
    relative = [] if normalized == "." else split_segments(normalized)

    combined: list[str] = list(base)
    for seg in relative:
        if seg == "..":
            if combined:
                combined.pop()
            # ".." que sobrepasa la raíz: se queda en la raíz (como Linux).
        else:
            combined.append(seg)
    return combined


def resolve_directory(db: Session, path: str, cwd_id: int | None = None) -> Directory:
    """Resuelve `path` (absoluto o relativo a `cwd_id`) a un Directory
    existente. Lanza NotFoundError si algún componente no existe, o
    NotADirectoryError si algún componente intermedio es en realidad un
    archivo."""
    segments = _resolve_segments(db, path, cwd_id)

    current = get_root_directory(db)
    walked = ""
    for seg in segments:
        walked += f"/{seg}"
        nxt = find_child_directory(db, current.id, seg)
        if nxt is None:
            if find_child_file(db, current.id, seg) is not None:
                raise NotADirectoryError(f"{walked} es un archivo, no un directorio")
            raise NotFoundError(f"No existe el directorio {walked}")
        current = nxt
    return current


def resolve_file(db: Session, path: str, cwd_id: int | None = None) -> File:
    """Resuelve `path` a un File existente. El último segmento debe ser un
    archivo; todos los anteriores deben ser directorios existentes."""
    parent_path, leaf = split_parent_and_leaf(path)
    parent_dir = resolve_directory(db, parent_path, cwd_id)

    file = find_child_file(db, parent_dir.id, leaf)
    if file is not None:
        return file
    if find_child_directory(db, parent_dir.id, leaf) is not None:
        raise NotAFileError(f"{path} es un directorio, no un archivo")
    raise NotFoundError(f"No existe el archivo {path}")


@dataclass(frozen=True)
class ResolvedParent:
    """Resultado de resolver la ruta de algo que se va a crear: el
    directorio padre (ya existente) y el nombre de la hoja a crear."""

    parent: Directory
    leaf_name: str


def resolve_parent_and_name(db: Session, path: str, cwd_id: int | None = None) -> ResolvedParent:
    """Para operaciones de creación (mkdir, put de archivo nuevo):
    resuelve y devuelve el directorio padre (que debe existir ya) y el
    nombre del nuevo hijo, con el nombre validado. NO comprueba si el
    hijo ya existe; eso lo decide el llamador según la operación (p. ej.
    mkdir podría fallar con AlreadyExistsError, put podría sobreescribir).
    """
    parent_path, leaf = split_parent_and_leaf(path)
    validate_segment_name(leaf)
    parent_dir = resolve_directory(db, parent_path, cwd_id)
    return ResolvedParent(parent=parent_dir, leaf_name=leaf)


def list_children(db: Session, directory: Directory) -> tuple[list[Directory], list[File]]:
    """Lista el contenido directo de un directorio: (subdirectorios, archivos).
    Pensado para que el compañero de FS lo use en su endpoint `ls`."""
    subdirs = list(
        db.scalars(
            select(Directory).where(Directory.parent_id == directory.id).order_by(Directory.name)
        )
    )
    files = list(
        db.scalars(select(File).where(File.parent_id == directory.id).order_by(File.name))
    )
    return subdirs, files
