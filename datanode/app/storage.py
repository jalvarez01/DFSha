"""Almacenamiento local de bloques de este DataNode.

Decisión de equipo (RNF4): "archivo como directorio, bloque como archivo".
En vez de un blob monolítico por archivo (como en el Hito 1), cada archivo
del DFS es una CARPETA en este DataNode (nombrada por su `file_id`, el que
asigna el ControlNode) y cada bloque de ese archivo es un ARCHIVO suelto
dentro de esa carpeta (nombrado por su `block_id` opaco). Consecuencia
directa del requisito "en un DataNode no debe estar el archivo completo":
ESTE nodo solo ve los bloques que le tocaron a él, nunca el archivo entero
reensamblado (eso solo pasa en el cliente).

    <storage_dir>/<file_id>/<block_id>

Un índice en memoria (`block_id -> file_id`) evita tener que buscar en qué
carpeta vive un bloque en GET/DELETE (el cliente solo manda el block_id,
ver CONTRATOS.md). Se reconstruye escaneando el disco al arrancar, así que
sobrevive a reinicios sin necesitar una base de datos aparte.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from threading import Lock


class BlockNotFoundError(Exception):
    pass


class BlockStore:
    def __init__(self, storage_dir: Path) -> None:
        self._root = storage_dir
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        # block_id -> file_id. Reconstruido escaneando <root>/<file_id>/<block_id>.
        self._index: dict[str, str] = {}
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        for file_dir in self._root.iterdir():
            if not file_dir.is_dir():
                continue
            for block_path in file_dir.iterdir():
                if block_path.is_file() and not block_path.name.endswith(".tmp"):
                    self._index[block_path.name] = file_dir.name

    def _path_for(self, file_id: str, block_id: str) -> Path:
        return self._root / file_id / block_id

    def put(self, block_id: str, file_id: str, data: bytes) -> tuple[int, str]:
        """Guarda (o reemplaza) el contenido de un bloque. Devuelve
        (tamaño_bytes, checksum_sha256_hex). Escritura atómica: escribe a un
        .tmp y hace rename, para no dejar un bloque a medias si el proceso
        muere a mitad de la escritura."""
        target = self._path_for(file_id, block_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        checksum = hashlib.sha256(data).hexdigest()
        tmp.write_bytes(data)
        tmp.replace(target)
        with self._lock:
            # Si el bloque ya existía bajo OTRO file_id (no debería pasar
            # con block_ids opacos únicos, pero por robustez), limpiar el
            # rastro viejo del índice.
            self._index[block_id] = file_id
        return len(data), checksum

    def get(self, block_id: str) -> bytes:
        path = self._locate(block_id)
        return path.read_bytes()

    def delete(self, block_id: str) -> bool:
        with self._lock:
            file_id = self._index.pop(block_id, None)
        if file_id is None:
            path = self._scan_for(block_id)
            if path is None:
                return False
        else:
            path = self._path_for(file_id, block_id)
        existed = path.exists()
        path.unlink(missing_ok=True)
        # Si la carpeta del archivo quedó vacía, la limpiamos (no es
        # obligatorio, pero evita acumular directorios vacíos).
        parent = path.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
        return existed

    def delete_file(self, file_id: str) -> int:
        """Borra TODOS los bloques de un archivo de un golpe (usado cuando
        RF1 propaga un `rm`/`rmdir --recursive` a los DataNodes). Devuelve
        cuántos bloques había."""
        file_dir = self._root / file_id
        if not file_dir.exists():
            return 0
        block_ids = [p.name for p in file_dir.iterdir() if p.is_file()]
        with self._lock:
            for block_id in block_ids:
                self._index.pop(block_id, None)
        shutil.rmtree(file_dir, ignore_errors=True)
        return len(block_ids)

    def _locate(self, block_id: str) -> Path:
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
            candidate = file_dir / block_id
            if candidate.is_file():
                with self._lock:
                    self._index[block_id] = file_dir.name
                return candidate
        return None
