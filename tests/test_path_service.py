"""
Tests unitarios del módulo app/path_service.py.

Se dividen en dos bloques, siguiendo las dos capas del módulo:
1. Funciones puras de string (normalize_path, split_segments,
   validate_segment_name, split_parent_and_leaf) — sin BD.
2. Funciones de resolución contra la BD (resolve_directory, resolve_file,
   resolve_parent_and_name, list_children, get_full_path) — usan la
   fixture `db` de conftest.py (SQLite en memoria).
"""
import pytest
from sqlalchemy.orm import Session

from app.models import Directory, File
from app.path_service import (
    AlreadyExistsError,
    InvalidPathError,
    NotADirectoryError,
    NotAFileError,
    NotFoundError,
    RootOperationError,
    get_full_path,
    list_children,
    normalize_path,
    resolve_directory,
    resolve_file,
    resolve_parent_and_name,
    split_parent_and_leaf,
    split_segments,
    validate_segment_name,
)

# --------------------------------------------------------------------------
# 1. validate_segment_name
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["a", "archivo.txt", "carpeta_1", "á-ñ", "a" * 255])
def test_validate_segment_name_acepta_nombres_validos(name: str) -> None:
    validate_segment_name(name)  # no debe lanzar


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "a/b", "a\x00b", "a" * 256],
)
def test_validate_segment_name_rechaza_nombres_invalidos(name: str) -> None:
    with pytest.raises(InvalidPathError):
        validate_segment_name(name)


# --------------------------------------------------------------------------
# 2. split_segments
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path,expected",
    [
        ("/a/b", ["a", "b"]),
        ("/a//b/", ["a", "b"]),
        ("a/./b", ["a", ".", "b"]),
        ("", []),
        ("/", []),
    ],
)
def test_split_segments(path: str, expected: list[str]) -> None:
    assert split_segments(path) == expected


# --------------------------------------------------------------------------
# 3. normalize_path
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path,expected",
    [
        ("/", "/"),
        ("/a/b/c", "/a/b/c"),
        ("/a//b///c", "/a/b/c"),
        ("/a/./b", "/a/b"),
        ("/a/b/../c", "/a/c"),
        ("/a/../../b", "/b"),  # ".." en la raíz absoluta es no-op
        ("a/b", "a/b"),
        ("./a/b", "a/b"),
        ("a/../b", "b"),
        ("..", ".."),  # relativo: no se puede colapsar sin cwd
        ("../a", "../a"),
        (".", "."),
    ],
)
def test_normalize_path(path: str, expected: str) -> None:
    assert normalize_path(path) == expected


def test_normalize_path_vacia_lanza_error() -> None:
    with pytest.raises(InvalidPathError):
        normalize_path("")


def test_normalize_path_propaga_nombre_invalido() -> None:
    with pytest.raises(InvalidPathError):
        normalize_path("/a/b\x01c")


# --------------------------------------------------------------------------
# 4. split_parent_and_leaf
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path,expected_parent,expected_leaf",
    [
        ("/a/b/c", "/a/b", "c"),
        ("/c", "/", "c"),
        ("a/b/c", "a/b", "c"),
        ("c", ".", "c"),
    ],
)
def test_split_parent_and_leaf(path: str, expected_parent: str, expected_leaf: str) -> None:
    parent, leaf = split_parent_and_leaf(path)
    assert (parent, leaf) == (expected_parent, expected_leaf)


def test_split_parent_and_leaf_raiz_lanza_root_operation_error() -> None:
    with pytest.raises(RootOperationError):
        split_parent_and_leaf("/")


def test_split_parent_and_leaf_termina_en_dotdot_lanza_error() -> None:
    with pytest.raises(InvalidPathError):
        split_parent_and_leaf("a/..")


# --------------------------------------------------------------------------
# 5. resolve_directory / resolve_file (requieren BD)
# --------------------------------------------------------------------------
def _mkdir(db: Session, owner_id: int, parent: Directory, name: str) -> Directory:
    d = Directory(name=name, parent_id=parent.id, owner_id=owner_id)
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _touch(db: Session, owner_id: int, parent: Directory, name: str, size: int = 0) -> File:
    f = File(name=name, parent_id=parent.id, owner_id=owner_id, size_bytes=size)
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def test_resolve_directory_raiz(db: Session) -> None:
    root = resolve_directory(db, "/")
    assert root.is_root
    assert get_full_path(db, root) == "/"


def test_resolve_directory_absoluta(db: Session, owner_id: int) -> None:
    root = resolve_directory(db, "/")
    a = _mkdir(db, owner_id, root, "a")
    b = _mkdir(db, owner_id, a, "b")

    resolved = resolve_directory(db, "/a/b")
    assert resolved.id == b.id
    assert get_full_path(db, resolved) == "/a/b"


def test_resolve_directory_no_existe(db: Session) -> None:
    with pytest.raises(NotFoundError):
        resolve_directory(db, "/no/existe")


def test_resolve_directory_componente_es_archivo(db: Session, owner_id: int) -> None:
    root = resolve_directory(db, "/")
    _touch(db, owner_id, root, "archivo.txt")

    with pytest.raises(NotADirectoryError):
        resolve_directory(db, "/archivo.txt/sub")


def test_resolve_directory_relativa_con_cwd(db: Session, owner_id: int) -> None:
    root = resolve_directory(db, "/")
    a = _mkdir(db, owner_id, root, "a")
    b = _mkdir(db, owner_id, a, "b")
    c = _mkdir(db, owner_id, b, "c")

    resolved = resolve_directory(db, "c", cwd_id=b.id)
    assert resolved.id == c.id


def test_resolve_directory_relativa_con_dotdot(db: Session, owner_id: int) -> None:
    root = resolve_directory(db, "/")
    a = _mkdir(db, owner_id, root, "a")
    b = _mkdir(db, owner_id, a, "b")
    sibling = _mkdir(db, owner_id, a, "sibling")

    resolved = resolve_directory(db, "../sibling", cwd_id=b.id)
    assert resolved.id == sibling.id


def test_resolve_directory_relativa_sin_cwd_lanza_error(db: Session) -> None:
    with pytest.raises(InvalidPathError):
        resolve_directory(db, "a/b")


def test_resolve_file_encuentra_archivo(db: Session, owner_id: int) -> None:
    root = resolve_directory(db, "/")
    a = _mkdir(db, owner_id, root, "a")
    f = _touch(db, owner_id, a, "datos.bin", size=42)

    resolved = resolve_file(db, "/a/datos.bin")
    assert resolved.id == f.id
    assert resolved.size_bytes == 42


def test_resolve_file_sobre_directorio_lanza_not_a_file(db: Session, owner_id: int) -> None:
    root = resolve_directory(db, "/")
    _mkdir(db, owner_id, root, "a")

    with pytest.raises(NotAFileError):
        resolve_file(db, "/a")


def test_resolve_file_no_existe(db: Session) -> None:
    with pytest.raises(NotFoundError):
        resolve_file(db, "/no_existe.txt")


# --------------------------------------------------------------------------
# 6. resolve_parent_and_name
# --------------------------------------------------------------------------
def test_resolve_parent_and_name(db: Session, owner_id: int) -> None:
    root = resolve_directory(db, "/")
    a = _mkdir(db, owner_id, root, "a")

    result = resolve_parent_and_name(db, "/a/nuevo")
    assert result.parent.id == a.id
    assert result.leaf_name == "nuevo"


def test_resolve_parent_and_name_padre_no_existe(db: Session) -> None:
    with pytest.raises(NotFoundError):
        resolve_parent_and_name(db, "/no/existe/nuevo")


# --------------------------------------------------------------------------
# 7. list_children
# --------------------------------------------------------------------------
def test_list_children(db: Session, owner_id: int) -> None:
    root = resolve_directory(db, "/")
    a = _mkdir(db, owner_id, root, "a")
    _mkdir(db, owner_id, root, "b")
    _touch(db, owner_id, root, "archivo.txt")

    subdirs, files = list_children(db, root)
    assert [d.name for d in subdirs] == ["a", "b"]
    assert [f.name for f in files] == ["archivo.txt"]


def test_directory_y_file_no_pueden_repetir_nombre_a_nivel_bd(db: Session, owner_id: int) -> None:
    """El UniqueConstraint (parent_id, name) impide duplicados dentro de
    la misma tabla; se documenta aquí como referencia de comportamiento
    esperado (la validación de "ya existe" a nivel de negocio la hace
    cada endpoint llamando primero a find_child_directory/find_child_file)."""
    root = resolve_directory(db, "/")
    _mkdir(db, owner_id, root, "dup")

    with pytest.raises(Exception):
        _mkdir(db, owner_id, root, "dup")
