import hashlib

import pytest

from app.storage import BlockNotFoundError, BlockStore


@pytest.fixture()
def store(tmp_path):
    return BlockStore(tmp_path)


def test_put_creates_directory_per_file_and_file_per_block(store, tmp_path):
    size, checksum = store.put("blk-1", "file-42", b"hola mundo")
    assert size == len(b"hola mundo")
    assert checksum == hashlib.sha256(b"hola mundo").hexdigest()
    # "archivo como directorio, bloque como archivo"
    assert (tmp_path / "file-42" / "blk-1").read_bytes() == b"hola mundo"


def test_get_roundtrip(store):
    store.put("blk-1", "file-42", b"contenido")
    assert store.get("blk-1") == b"contenido"


def test_get_missing_raises(store):
    with pytest.raises(BlockNotFoundError):
        store.get("no-existe")


def test_put_overwrite_replaces_content(store):
    store.put("blk-1", "file-42", b"version vieja")
    store.put("blk-1", "file-42", b"version nueva")
    assert store.get("blk-1") == b"version nueva"


def test_delete_removes_block(store):
    store.put("blk-1", "file-42", b"data")
    assert store.delete("blk-1") is True
    with pytest.raises(BlockNotFoundError):
        store.get("blk-1")


def test_delete_missing_returns_false(store):
    assert store.delete("no-existe") is False


def test_delete_empties_file_directory(store, tmp_path):
    store.put("blk-1", "file-42", b"data")
    store.delete("blk-1")
    assert not (tmp_path / "file-42").exists()


def test_delete_file_removes_all_its_blocks(store):
    store.put("blk-1", "file-42", b"a")
    store.put("blk-2", "file-42", b"b")
    store.put("blk-3", "file-7", b"c")
    deleted = store.delete_file("file-42")
    assert deleted == 2
    with pytest.raises(BlockNotFoundError):
        store.get("blk-1")
    # el bloque de OTRO archivo no se toca
    assert store.get("blk-3") == b"c"


def test_delete_file_unknown_returns_zero(store):
    assert store.delete_file("no-existe") == 0


def test_index_survives_restart(tmp_path):
    first = BlockStore(tmp_path)
    first.put("blk-1", "file-42", b"persistente")

    second = BlockStore(tmp_path)  # simula reinicio: reconstruye del disco
    assert second.get("blk-1") == b"persistente"
