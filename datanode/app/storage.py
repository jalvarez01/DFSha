"""Almacenamiento local de bloques de este DataNode.

Decisión de equipo (RNF4): "archivo como directorio, bloque como archivo".
Cada archivo del DFS es una CARPETA en este DataNode (nombrada por su
`file_id`, el que asigna el ControlNode) y cada bloque de ese archivo es un
ARCHIVO suelto dentro de esa carpeta (nombrado por su `block_id` opaco).
ESTE nodo solo ve los bloques que le tocaron, nunca el archivo entero.

    <storage_dir>/<file_id>/<block_id>          contenido del bloque
    <storage_dir>/<file_id>/<block_id>.sha256   SHA-256 (hex) del contenido EN CLARO

Seguridad: `file_id` y `block_id` llegan de la red y se usan para armar
rutas en disco, así que se validan contra `_ID_PATTERN` (solo letras,
dígitos, `_` y `-`). Sin eso, un `file_id` como `../..` permitiría escribir
o borrar fuera de `storage_dir`.

Cifrado en reposo (Hito 3, RNF6): si hay clave (`DFSHA_DATANODE_STORAGE_KEY`),
cada bloque se guarda cifrado con AES-256-GCM, en trozos de 64 KiB para no
cargarlo entero en memoria:

    "DFSE1\\n" | prefijo de nonce (7 bytes aleatorios) | trozo_0 | trozo_1 | ...
    trozo_i = AES-GCM(clave, nonce = prefijo | ¿último? (1 byte) | i (4 bytes),
                      texto = 64 KiB de contenido (el último, lo que quede),
                      datos asociados = "DFSE1\\n")  -> texto cifrado + etiqueta de 16 bytes

El byte "¿último?" va dentro del nonce: si alguien trunca el archivo, el
trozo que queda al final no se descifra como último y la lectura falla
(se detecta igual que cualquier otra corrupción). Los bloques escritos sin
cifrar (antes de activar la clave) se siguen leyendo.

Integridad: el checksum se calcula en streaming al escribir y se guarda al
lado del bloque. Si el cliente manda el checksum esperado, un bloque que
no coincide se rechaza sin llegar a guardarse. Al leer, el bloque se
verifica contra su checksum antes de servirse.

Inmutabilidad (Hito 3, consistencia): un bloque ya guardado no se puede
sobrescribir con otro contenido (`BlockExistsError`). Un PUT repetido con el
mismo contenido es un no-op (reintentos idempotentes). Contenido nuevo =
`block_id` nuevo, así que un lector nunca ve un bloque cambiar a mitad.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CHUNK_SIZE = 64 * 1024

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CHECKSUM_SUFFIX = ".sha256"
_TMP_SUFFIX = ".tmp"

_MAGIC = b"DFSE1\n"
_PREFIX_LEN = 7
_HEADER_LEN = len(_MAGIC) + _PREFIX_LEN
_TAG_LEN = 16
_CIPHER_CHUNK = CHUNK_SIZE + _TAG_LEN


class BlockNotFoundError(Exception):
    pass


class InvalidIdError(ValueError):
    """Un file_id/block_id con caracteres no permitidos."""


class ChecksumMismatchError(Exception):
    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"checksum esperado {expected}, obtenido {actual}")
        self.expected = expected
        self.actual = actual


class BlockExistsError(Exception):
    """Ya hay un bloque con ese id y otro contenido (los bloques son inmutables)."""


class CorruptBlockError(Exception):
    """El contenido en disco ya no coincide con su checksum (o no se descifra)."""


def validate_id(value: str, kind: str = "id") -> str:
    if not _ID_PATTERN.fullmatch(value):
        raise InvalidIdError(f"{kind} inválido: {value!r} (solo letras, dígitos, '_' y '-', máx. 128)")
    return value


def load_key(encoded: str) -> bytes | None:
    """Clave AES-256 desde base64 (vacío = sin cifrado)."""
    if not encoded:
        return None
    key = base64.b64decode(encoded)
    if len(key) != 32:
        raise ValueError("DFSHA_DATANODE_STORAGE_KEY debe ser base64 de 32 bytes (AES-256)")
    return key


def _nonce(prefix: bytes, index: int, last: bool) -> bytes:
    return prefix + (b"\x01" if last else b"\x00") + index.to_bytes(4, "big")


class _EncryptingWriter:
    def __init__(self, f, key: bytes) -> None:
        self._f = f
        self._aead = AESGCM(key)
        self._prefix = os.urandom(_PREFIX_LEN)
        self._buffer = bytearray()
        self._index = 0
        f.write(_MAGIC + self._prefix)

    def write(self, data: bytes) -> None:
        self._buffer.extend(data)
        # Un trozo solo se emite como "no último" si sabemos que viene más.
        while len(self._buffer) > CHUNK_SIZE:
            self._emit(bytes(self._buffer[:CHUNK_SIZE]), last=False)
            del self._buffer[:CHUNK_SIZE]

    def _emit(self, plain: bytes, last: bool) -> None:
        self._f.write(self._aead.encrypt(_nonce(self._prefix, self._index, last), plain, _MAGIC))
        self._index += 1

    def close(self) -> None:
        self._emit(bytes(self._buffer), last=True)
        self._buffer.clear()


@dataclass
class PendingWrite:
    """Escritura en curso de un bloque: el llamador va pasando chunks con
    `write` y termina con `BlockStore.commit` o `BlockStore.abort`. Existe
    para que la API async pueda escribir el cuerpo en streaming sin que este
    módulo dependa de asyncio."""

    block_id: str
    file_id: str
    tmp_path: Path
    _file: object
    _hasher: object
    _encryptor: _EncryptingWriter | None = None
    size: int = 0

    def write(self, chunk: bytes) -> None:
        if self._encryptor is not None:
            self._encryptor.write(chunk)
        else:
            self._file.write(chunk)
        self._hasher.update(chunk)
        self.size += len(chunk)

    def _close(self) -> None:
        if self._encryptor is not None:
            self._encryptor.close()
        self._file.close()


class BlockStore:
    def __init__(self, storage_dir: Path, key: bytes | None = None) -> None:
        self._root = storage_dir
        self._root.mkdir(parents=True, exist_ok=True)
        self._key = key
        self._lock = Lock()
        # block_id -> file_id. Reconstruido escaneando <root>/<file_id>/<block_id>.
        self._index: dict[str, str] = {}
        self._rebuild_index()

    @property
    def encrypted(self) -> bool:
        return self._key is not None

    @staticmethod
    def _is_block_file(path: Path) -> bool:
        return (
            path.is_file()
            and not path.name.endswith(_TMP_SUFFIX)
            and not path.name.endswith(_CHECKSUM_SUFFIX)
            and _ID_PATTERN.fullmatch(path.name) is not None
        )

    def _rebuild_index(self) -> None:
        for file_dir in self._root.iterdir():
            if not file_dir.is_dir():
                continue
            for block_path in file_dir.iterdir():
                if self._is_block_file(block_path):
                    self._index[block_path.name] = file_dir.name
                elif block_path.name.endswith(_TMP_SUFFIX):
                    # Restos de una escritura interrumpida por un reinicio.
                    block_path.unlink(missing_ok=True)

    def _path_for(self, file_id: str, block_id: str) -> Path:
        return self._root / file_id / block_id

    @staticmethod
    def _checksum_path(block_path: Path) -> Path:
        return block_path.with_name(block_path.name + _CHECKSUM_SUFFIX)

    # -- escritura ---------------------------------------------------------
    def begin(self, block_id: str, file_id: str) -> PendingWrite:
        validate_id(block_id, "block_id")
        validate_id(file_id, "file_id")
        target = self._path_for(file_id, block_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Nombre de temporal único: dos subidas concurrentes del mismo
        # bloque no se pisan el archivo a medio escribir.
        tmp = target.with_name(f"{block_id}.{uuid.uuid4().hex}{_TMP_SUFFIX}")
        f = open(tmp, "wb")
        return PendingWrite(
            block_id=block_id,
            file_id=file_id,
            tmp_path=tmp,
            _file=f,
            _hasher=hashlib.sha256(),
            _encryptor=_EncryptingWriter(f, self._key) if self._key else None,
        )

    def abort(self, pending: PendingWrite) -> None:
        try:
            pending._file.close()
        finally:
            pending.tmp_path.unlink(missing_ok=True)

    def commit(self, pending: PendingWrite, expected_checksum: str | None = None) -> tuple[int, str]:
        """Cierra la escritura. Si `expected_checksum` no coincide, descarta
        el temporal y lanza ChecksumMismatchError. Si el bloque ya existía
        con el mismo contenido, es un no-op; con otro, BlockExistsError.
        Rename atómico: nunca queda un bloque a medias."""
        pending._close()
        checksum = pending._hasher.hexdigest()
        if expected_checksum is not None and checksum != expected_checksum.lower():
            pending.tmp_path.unlink(missing_ok=True)
            raise ChecksumMismatchError(expected_checksum, checksum)

        with self._lock:
            existing = self._locate_unlocked(pending.block_id)
            if existing is not None:
                pending.tmp_path.unlink(missing_ok=True)
                if self._stored_checksum(existing) == checksum:
                    return pending.size, checksum
                raise BlockExistsError(f"El bloque {pending.block_id} ya existe con otro contenido")
            target = self._path_for(pending.file_id, pending.block_id)
            self._checksum_path(target).write_text(checksum)
            pending.tmp_path.replace(target)
            self._index[pending.block_id] = pending.file_id
        return pending.size, checksum

    def put_stream(
        self,
        block_id: str,
        file_id: str,
        chunks: Iterable[bytes],
        expected_checksum: str | None = None,
    ) -> tuple[int, str]:
        pending = self.begin(block_id, file_id)
        try:
            for chunk in chunks:
                pending.write(chunk)
        except BaseException:
            self.abort(pending)
            raise
        return self.commit(pending, expected_checksum)

    def put(self, block_id: str, file_id: str, data: bytes, expected_checksum: str | None = None) -> tuple[int, str]:
        """Guarda el contenido de un bloque. Devuelve (tamaño_bytes, checksum_sha256_hex)."""
        return self.put_stream(block_id, file_id, [data], expected_checksum)

    # -- lectura -----------------------------------------------------------
    def _stored_checksum(self, path: Path) -> str:
        checksum_path = self._checksum_path(path)
        if checksum_path.exists():
            return checksum_path.read_text().strip()
        return self._hash(path)

    def _hash(self, path: Path) -> str:
        hasher = hashlib.sha256()
        for chunk in self.iter_block(path):
            hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def _is_encrypted_file(path: Path) -> bool:
        with open(path, "rb") as f:
            return f.read(len(_MAGIC)) == _MAGIC

    def plain_size(self, path: Path) -> int:
        size = path.stat().st_size
        if not self._is_encrypted_file(path):
            return size
        body = size - _HEADER_LEN
        chunks = max(1, -(-body // _CIPHER_CHUNK))
        return body - chunks * _TAG_LEN

    def iter_block(self, path: Path) -> Iterator[bytes]:
        """Contenido EN CLARO del bloque, por chunks (descifrando si hace falta)."""
        with open(path, "rb") as f:
            head = f.read(len(_MAGIC))
            if head != _MAGIC:
                if head:
                    yield head
                for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                    yield chunk
                return
            if self._key is None:
                raise CorruptBlockError(f"{path.name} está cifrado y este nodo no tiene la clave")
            aead = AESGCM(self._key)
            prefix = f.read(_PREFIX_LEN)
            current = f.read(_CIPHER_CHUNK)
            index = 0
            while True:
                following = f.read(_CIPHER_CHUNK)
                last = not following
                try:
                    yield aead.decrypt(_nonce(prefix, index, last), current, _MAGIC)
                except InvalidTag as exc:
                    raise CorruptBlockError(f"{path.name} no se puede descifrar (alterado o truncado)") from exc
                if last:
                    return
                current = following
                index += 1

    def open_verified(self, block_id: str) -> tuple[Path, int, str]:
        """Localiza un bloque y verifica su integridad. Devuelve
        (ruta, tamaño en claro, checksum). Lanza BlockNotFoundError o
        CorruptBlockError."""
        path = self._locate(block_id)
        actual = self._hash(path)
        checksum_path = self._checksum_path(path)
        if checksum_path.exists():
            stored = checksum_path.read_text().strip()
            if stored != actual:
                raise CorruptBlockError(
                    f"el bloque {block_id} no coincide con su checksum (guardado={stored}, en disco={actual})"
                )
        return path, self.plain_size(path), actual

    def get(self, block_id: str) -> bytes:
        path, _, _ = self.open_verified(block_id)
        return b"".join(self.iter_block(path))

    def inventory(self) -> list[dict]:
        """Todos los bloques de este nodo: [{block_id, checksum, size_bytes}].
        Usa el checksum guardado (no relee los bloques)."""
        with self._lock:
            items = list(self._index.items())
        out = []
        for block_id, file_id in items:
            path = self._path_for(file_id, block_id)
            if not path.exists():
                continue
            out.append({"block_id": block_id, "checksum": self._stored_checksum(path), "size_bytes": self.plain_size(path)})
        return out

    def file_id_of(self, block_id: str) -> str | None:
        path = self._locate_or_none(block_id)
        return path.parent.name if path else None

    # -- borrado -----------------------------------------------------------
    def delete(self, block_id: str) -> bool:
        validate_id(block_id, "block_id")
        with self._lock:
            file_id = self._index.pop(block_id, None)
        if file_id is None:
            path = self._scan_for(block_id)
            if path is None:
                return False
            with self._lock:
                self._index.pop(block_id, None)
        else:
            path = self._path_for(file_id, block_id)
        existed = path.exists()
        path.unlink(missing_ok=True)
        self._checksum_path(path).unlink(missing_ok=True)
        # Si la carpeta del archivo quedó vacía, la limpiamos.
        parent = path.parent
        try:
            if parent != self._root and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass  # otra escritura concurrente acaba de crear algo dentro
        return existed

    def delete_file(self, file_id: str) -> int:
        """Borra TODOS los bloques de un archivo de un golpe. Devuelve
        cuántos bloques había."""
        validate_id(file_id, "file_id")
        file_dir = self._root / file_id
        if not file_dir.is_dir():
            return 0
        block_ids = [p.name for p in file_dir.iterdir() if self._is_block_file(p)]
        with self._lock:
            for block_id in block_ids:
                self._index.pop(block_id, None)
        shutil.rmtree(file_dir, ignore_errors=True)
        return len(block_ids)

    # -- búsqueda ------------------------------------------------------------
    def _locate_unlocked(self, block_id: str) -> Path | None:
        file_id = self._index.get(block_id)
        if file_id is not None:
            path = self._path_for(file_id, block_id)
            if path.exists():
                return path
        return None

    def _locate_or_none(self, block_id: str) -> Path | None:
        validate_id(block_id, "block_id")
        with self._lock:
            path = self._locate_unlocked(block_id)
        return path or self._scan_for(block_id)

    def _locate(self, block_id: str) -> Path:
        path = self._locate_or_none(block_id)
        if path is None:
            raise BlockNotFoundError(block_id)
        return path

    def _scan_for(self, block_id: str) -> Path | None:
        """Fallback si el índice en memoria y el disco quedaron
        desincronizados (no debería pasar en operación normal)."""
        for file_dir in self._root.iterdir():
            if not file_dir.is_dir():
                continue
            candidate = file_dir / block_id
            if candidate.is_file():
                with self._lock:
                    self._index[block_id] = file_dir.name
                return candidate
        return None
