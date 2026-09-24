"""Almacenamiento local de bloques de este DataNode.

Decisión de equipo (RNF4): "archivo como directorio, bloque como archivo".
En vez de un blob monolítico por archivo (como en el Hito 1), cada archivo
del DFS es una CARPETA en este DataNode (nombrada por su `file_id`, el que
asigna el ControlNode) y cada bloque de ese archivo es un ARCHIVO suelto
dentro de esa carpeta (nombrado por su `block_id` opaco). Consecuencia
directa del requisito "en un DataNode no debe estar el archivo completo":
ESTE nodo solo ve los bloques que le tocaron a él, nunca el archivo entero
reensamblado (eso solo pasa en el cliente).

    <storage_dir>/<file_id>/<block_id>          contenido del bloque
    <storage_dir>/<file_id>/<block_id>.sha256   su checksum SHA-256 (hex)

Seguridad: `file_id` y `block_id` llegan de la red y se usan para armar
rutas en disco, así que se validan contra `_ID_PATTERN` (solo letras,
dígitos, `_` y `-`). Sin eso, un `file_id` como `../..` permitiría escribir
o borrar fuera de `storage_dir`.

Integridad: el checksum se calcula en streaming al escribir y se guarda al
lado del bloque. Si el cliente manda el checksum esperado, un bloque que
no coincide se rechaza sin llegar a guardarse. Al leer, el bloque se
verifica contra su checksum antes de servirse: un bloque corrupto en disco
nunca sale del nodo.

Un índice en memoria (`block_id -> file_id`) evita tener que buscar en qué
carpeta vive un bloque en GET/DELETE (el cliente solo manda el block_id,
ver CONTRATOS.md). Se reconstruye escaneando el disco al arrancar, así que
sobrevive a reinicios sin necesitar una base de datos aparte.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

CHUNK_SIZE = 64 * 1024

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CHECKSUM_SUFFIX = ".sha256"
_TMP_SUFFIX = ".tmp"


class BlockNotFoundError(Exception):
    pass


class InvalidIdError(ValueError):
    """Un file_id/block_id con caracteres no permitidos."""


class ChecksumMismatchError(Exception):
    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"checksum esperado {expected}, obtenido {actual}")
        self.expected = expected
        self.actual = actual


class CorruptBlockError(Exception):
    """El contenido en disco ya no coincide con el checksum guardado."""


def validate_id(value: str, kind: str = "id") -> str:
    if not _ID_PATTERN.fullmatch(value):
        raise InvalidIdError(
            f"{kind} inválido: {value!r} (solo letras, dígitos, '_' y '-', máx. 128)"
        )
    return value


def sha256_of(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


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
    size: int = 0

    def write(self, chunk: bytes) -> None:
        self._file.write(chunk)
        self._hasher.update(chunk)
        self.size += len(chunk)


class BlockStore:
    def __init__(self, storage_dir: Path) -> None:
        self._root = storage_dir
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        # block_id -> file_id. Reconstruido escaneando <root>/<file_id>/<block_id>.
        self._index: dict[str, str] = {}
        self._rebuild_index()

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
        return PendingWrite(
            block_id=block_id,
            file_id=file_id,
            tmp_path=tmp,
            _file=open(tmp, "wb"),
            _hasher=hashlib.sha256(),
        )

    def abort(self, pending: PendingWrite) -> None:
        pending._file.close()
        pending.tmp_path.unlink(missing_ok=True)

    def commit(self, pending: PendingWrite, expected_checksum: str | None = None) -> tuple[int, str]:
        """Cierra la escritura. Si `expected_checksum` no coincide, descarta
        el temporal y lanza ChecksumMismatchError (el bloque anterior, si
        había, queda intacto). Rename atómico: nunca queda un bloque a medias."""
        pending._file.close()
        checksum = pending._hasher.hexdigest()
        if expected_checksum is not None and checksum != expected_checksum.lower():
            pending.tmp_path.unlink(missing_ok=True)
            raise ChecksumMismatchError(expected_checksum, checksum)

        target = self._path_for(pending.file_id, pending.block_id)
        with self._lock:
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
        """Guarda (o reemplaza) el contenido de un bloque. Devuelve
        (tamaño_bytes, checksum_sha256_hex)."""
        return self.put_stream(block_id, file_id, [data], expected_checksum)

    # -- lectura -----------------------------------------------------------
    def open_verified(self, block_id: str) -> tuple[Path, int, str]:
        """Localiza un bloque y verifica su integridad. Devuelve
        (ruta, tamaño, checksum). Lanza BlockNotFoundError o
        CorruptBlockError."""
        path = self._locate(block_id)
        actual = sha256_of(path)
        checksum_path = self._checksum_path(path)
        if checksum_path.exists():
            stored = checksum_path.read_text().strip()
            if stored != actual:
                raise CorruptBlockError(
                    f"el bloque {block_id} no coincide con su checksum (guardado={stored}, en disco={actual})"
                )
        return path, path.stat().st_size, actual

    def iter_block(self, path: Path) -> Iterator[bytes]:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                yield chunk

    def get(self, block_id: str) -> bytes:
        path, _, _ = self.open_verified(block_id)
        return path.read_bytes()

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
        # Si la carpeta del archivo quedó vacía, la limpiamos (no es
        # obligatorio, pero evita acumular directorios vacíos).
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

    def _locate(self, block_id: str) -> Path:
        validate_id(block_id, "block_id")
        with self._lock:
            file_id = self._index.get(block_id)
        if file_id is not None:
            path = self._path_for(file_id, block_id)
            if path.exists():
                return path
        path = self._scan_for(block_id)
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
