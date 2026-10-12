import hashlib

import pytest

from app.storage import (
    BlockExistsError,
    BlockNotFoundError,
    BlockStore,
    ChecksumMismatchError,
    CorruptBlockError,
    InvalidIdError,
)


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


def test_un_bloque_es_inmutable(store):
    store.put("blk-1", "file-42", b"version vieja")
    with pytest.raises(BlockExistsError):
        store.put("blk-1", "file-42", b"version nueva")
    assert store.get("blk-1") == b"version vieja"
    # El mismo contenido otra vez es un no-op (reintento idempotente).
    store.put("blk-1", "file-42", b"version vieja")


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


def test_ids_invalidos_se_rechazan_sin_tocar_disco(store, tmp_path):
    outside = tmp_path.parent / "fuera-del-storage"
    for file_id in ("..", "../..", "../fuera-del-storage", "a/b"):
        with pytest.raises(InvalidIdError):
            store.put("escapado", file_id, b"x")
    with pytest.raises(InvalidIdError):
        store.put("../escapado", "file-1", b"x")
    assert not outside.exists()
    assert not (tmp_path.parent / "escapado").exists()


def test_delete_file_con_traversal_no_borra_nada(store, tmp_path):
    victim = tmp_path.parent / "otro-directorio"
    victim.mkdir(exist_ok=True)
    with pytest.raises(InvalidIdError):
        store.delete_file("../otro-directorio")
    assert victim.exists()


def test_checksum_incorrecto_no_guarda_el_bloque(store, tmp_path):
    with pytest.raises(ChecksumMismatchError):
        store.put("blk-1", "file-42", b"data", expected_checksum="0" * 64)
    with pytest.raises(BlockNotFoundError):
        store.get("blk-1")
    # Tampoco quedan temporales.
    assert [p.name for p in (tmp_path / "file-42").iterdir()] == []


def test_checksum_se_guarda_junto_al_bloque_y_no_cuenta_como_bloque(tmp_path):
    first = BlockStore(tmp_path)
    first.put("blk-1", "file-42", b"data")
    assert (tmp_path / "file-42" / "blk-1.sha256").read_text() == hashlib.sha256(b"data").hexdigest()
    second = BlockStore(tmp_path)
    assert second.delete_file("file-42") == 1


def test_bloque_alterado_en_disco_se_detecta(store, tmp_path):
    store.put("blk-1", "file-42", b"original")
    (tmp_path / "file-42" / "blk-1").write_bytes(b"alterado")
    with pytest.raises(CorruptBlockError):
        store.get("blk-1")


def test_put_stream_escribe_por_chunks(store):
    size, checksum = store.put_stream("blk-1", "file-42", [b"ab", b"cd", b"ef"])
    assert size == 6
    assert checksum == hashlib.sha256(b"abcdef").hexdigest()
    assert store.get("blk-1") == b"abcdef"


def test_delete_borra_tambien_el_checksum(store, tmp_path):
    store.put("blk-1", "file-42", b"a")
    store.put("blk-2", "file-42", b"b")
    store.delete("blk-1")
    assert sorted(p.name for p in (tmp_path / "file-42").iterdir()) == ["blk-2", "blk-2.sha256"]


def test_temporales_huerfanos_se_limpian_al_arrancar(tmp_path):
    (tmp_path / "file-42").mkdir()
    (tmp_path / "file-42" / "blk-1.abc.tmp").write_bytes(b"a medias")
    BlockStore(tmp_path)
    assert list((tmp_path / "file-42").iterdir()) == []


# --------------------------------------------------------------------------
# Cifrado en reposo (AES-256-GCM)
# --------------------------------------------------------------------------
@pytest.fixture()
def encrypted(tmp_path):
    import os

    return BlockStore(tmp_path, key=os.urandom(32))


@pytest.mark.parametrize("size", [0, 1, 65535, 65536, 65537, 300_000])
def test_cifrado_ida_y_vuelta(encrypted, tmp_path, size):
    import os

    data = os.urandom(size)
    encrypted.put("blk-1", "f", data)
    assert encrypted.get("blk-1") == data
    assert encrypted.inventory()[0]["size_bytes"] == size


def test_en_disco_no_queda_el_texto_en_claro(encrypted, tmp_path):
    encrypted.put("blk-1", "f", b"DATO-CONFIDENCIAL-" * 1000)
    assert b"DATO-CONFIDENCIAL" not in (tmp_path / "f" / "blk-1").read_bytes()
    assert encrypted.encrypted


def test_bloque_cifrado_alterado_se_detecta(encrypted, tmp_path):
    encrypted.put("blk-1", "f", b"x" * 1000)
    path = tmp_path / "f" / "blk-1"
    raw = bytearray(path.read_bytes())
    raw[50] ^= 0xFF
    path.write_bytes(bytes(raw))
    with pytest.raises(CorruptBlockError):
        encrypted.get("blk-1")


def test_bloque_cifrado_truncado_se_detecta(encrypted, tmp_path):
    encrypted.put("blk-1", "f", b"x" * 70_000)  # dos trozos
    path = tmp_path / "f" / "blk-1"
    path.write_bytes(path.read_bytes()[: 13 + 65536 + 16])  # sin el trozo final
    with pytest.raises(CorruptBlockError):
        encrypted.get("blk-1")


def test_con_otra_clave_no_se_lee(encrypted, tmp_path):
    import os

    encrypted.put("blk-1", "f", b"secreto")
    with pytest.raises(CorruptBlockError):
        BlockStore(tmp_path, key=os.urandom(32)).get("blk-1")


def test_bloques_viejos_sin_cifrar_se_siguen_leyendo(tmp_path):
    import os

    BlockStore(tmp_path).put("viejo", "f", b"en claro")
    store = BlockStore(tmp_path, key=os.urandom(32))
    assert store.get("viejo") == b"en claro"


def test_clave_mal_formada_se_rechaza():
    import base64

    from app.storage import load_key

    with pytest.raises(ValueError):
        load_key(base64.b64encode(b"corta").decode())
    assert load_key("") is None
