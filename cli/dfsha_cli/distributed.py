"""
RF2 distribuido (Hito 2): put/get por bloques contra ControlNode + DataNodes.

El usuario sigue escribiendo `dfsha put archivo.txt` sin saber dónde caen
los bloques (transparencia de localización). Por debajo:

put (escritura en dos fases)
    1. POST {controlnode}/files/{ruta}/allocate con el tamaño del archivo
       -> plan: qué bloque va a qué DataNode.
    2. Para cada bloque, EN PARALELO: se calcula su SHA-256 leyendo solo
       ese tramo del archivo y se sube en streaming a
       PUT {datanode}/blocks/{block_id}?file_id=... con el header
       X-Checksum-Sha256 (el DataNode lo rechaza si llega distinto).
    3. POST {controlnode}/files/{ruta}/confirm con checksums y tamaños.

get
    1. GET {controlnode}/files/{ruta}/blocks -> plan de lectura ordenado.
    2. Se crea el archivo destino con su tamaño final y cada bloque se
       descarga EN PARALELO de su DataNode, escribiéndose directamente en
       su offset mientras se calcula su checksum.
    3. Si un bloque no coincide con el checksum del plan, se aborta y no
       queda ningún archivo a medias (se escribe a un temporal que solo se
       renombra al final).

Memoria: nunca se carga un bloque entero; todo va en chunks de CHUNK_SIZE,
así que el consumo no depende del tamaño del archivo ni del bloque.

El JWT de la sesión se manda tanto al ControlNode como a cada DataNode,
que lo valida con el secreto compartido.
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from dfsha_cli.config import CHUNK_SIZE, PARALLEL_TRANSFERS, UPLOAD_DOWNLOAD_TIMEOUT

ProgressCallback = Callable[[int], None]


@dataclass
class BlockTransferError(Exception):
    """Falló la transferencia de un bloque con un DataNode."""

    index: int
    node: str
    detail: str

    def __str__(self) -> str:
        return f"Bloque {self.index} en {self.node}: {self.detail}"


@dataclass
class TransferSummary:
    path: str
    size_bytes: int
    num_blocks: int
    # node_id -> cuántos bloques quedaron/salieron de ese nodo
    blocks_per_node: dict[str, int] = field(default_factory=dict)
    complete: bool = True


def datanode_url(location: dict) -> str:
    return f"http://{location['host']}:{location['port']}"


def _node_label(location: dict) -> str:
    return f"{location['node_id']} ({location['host']}:{location['port']})"


def _count_by_node(blocks: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for block in blocks:
        node = block["datanode"]["node_id"]
        counts[node] = counts.get(node, 0) + 1
    return counts


def _read_range(local_path: Path, offset: int, length: int) -> Iterator[bytes]:
    with open(local_path, "rb") as f:
        f.seek(offset)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                raise BlockTransferError(
                    index=-1, node="local", detail=f"{local_path} cambió de tamaño durante la subida"
                )
            remaining -= len(chunk)
            yield chunk


def _sha256_range(local_path: Path, offset: int, length: int) -> str:
    hasher = hashlib.sha256()
    for chunk in _read_range(local_path, offset, length):
        hasher.update(chunk)
    return hasher.hexdigest()


def _error_detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:200]}"
    if isinstance(detail, dict) and "message" in detail:
        return f"[{resp.status_code} {detail.get('error')}] {detail['message']}"
    return f"HTTP {resp.status_code}: {detail}"


class DistributedTransfer:
    """put/get por bloques. `control` es el DFShaClient ya autenticado (lo
    usa para hablar con el ControlNode); `http` es el cliente con el que se
    habla con los DataNodes (inyectable para tests)."""

    def __init__(
        self,
        control,
        http: httpx.Client | None = None,
        parallelism: int = PARALLEL_TRANSFERS,
    ) -> None:
        self.control = control
        self.parallelism = max(1, parallelism)
        self._owns_http = http is None
        self.http = http or httpx.Client(timeout=UPLOAD_DOWNLOAD_TIMEOUT)
        self.auth_headers = dict(control.auth_headers)

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> "DistributedTransfer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- put ---------------------------------------------------------------
    def put(
        self,
        remote_path: str,
        local_path: Path,
        block_size: int | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> TransferSummary:
        size = local_path.stat().st_size
        plan = self.control.allocate(remote_path, size, block_size)

        offsets: list[int] = []
        offset = 0
        for block in plan["blocks"]:
            offsets.append(offset)
            offset += block["size_bytes"]
        if offset != size:
            raise BlockTransferError(
                index=-1, node="controlnode", detail=f"el plan suma {offset} bytes y el archivo tiene {size}"
            )

        def upload(item: tuple[dict, int]) -> dict:
            block, block_offset = item
            return self._upload_block(local_path, block, block_offset, progress_cb)

        confirmations = self._run_parallel(upload, list(zip(plan["blocks"], offsets)))
        confirmed = self.control.confirm(remote_path, confirmations)
        return TransferSummary(
            path=confirmed["path"],
            size_bytes=confirmed["size_bytes"],
            num_blocks=confirmed["num_blocks"],
            blocks_per_node=_count_by_node(plan["blocks"]),
            complete=confirmed["complete"],
        )

    def _upload_block(
        self, local_path: Path, block: dict, offset: int, progress_cb: ProgressCallback | None
    ) -> dict:
        location = block["datanode"]
        length = block["size_bytes"]
        checksum = _sha256_range(local_path, offset, length)

        def body() -> Iterator[bytes]:
            for chunk in _read_range(local_path, offset, length):
                if progress_cb:
                    progress_cb(len(chunk))
                yield chunk

        url = f"{datanode_url(location)}/blocks/{block['block_id']}"
        try:
            resp = self.http.put(
                url,
                params={"file_id": str(block["file_id"])},
                content=body(),
                headers={
                    **self.auth_headers,
                    "X-Checksum-Sha256": checksum,
                    "Content-Length": str(length),
                },
            )
        except httpx.HTTPError as exc:
            raise BlockTransferError(block["index"], _node_label(location), f"no se pudo conectar: {exc}") from exc
        if not resp.is_success:
            raise BlockTransferError(block["index"], _node_label(location), _error_detail(resp))
        return {"block_id": block["block_id"], "checksum": checksum, "size_bytes": length}

    # -- get ---------------------------------------------------------------
    def get(
        self,
        remote_path: str,
        local_path: Path,
        progress_cb: ProgressCallback | None = None,
    ) -> TransferSummary:
        plan = self.control.blocks(remote_path)
        blocks = sorted(plan["blocks"], key=lambda b: b["index"])
        total = sum(b["size_bytes"] for b in blocks)

        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = local_path.with_name(f".{local_path.name}.dfsha-part")
        with open(tmp_path, "wb") as f:
            f.truncate(total)

        offsets: list[int] = []
        offset = 0
        for block in blocks:
            offsets.append(offset)
            offset += block["size_bytes"]

        def download(item: tuple[dict, int]) -> None:
            block, block_offset = item
            self._download_block(tmp_path, block, block_offset, progress_cb)

        try:
            self._run_parallel(download, list(zip(blocks, offsets)))
            os.replace(tmp_path, local_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        return TransferSummary(
            path=plan["path"],
            size_bytes=total,
            num_blocks=len(blocks),
            blocks_per_node=_count_by_node(blocks),
        )

    def _download_block(
        self, tmp_path: Path, block: dict, offset: int, progress_cb: ProgressCallback | None
    ) -> None:
        location = block["datanode"]
        label = _node_label(location)
        url = f"{datanode_url(location)}/blocks/{block['block_id']}"
        hasher = hashlib.sha256()
        written = 0
        try:
            with self.http.stream("GET", url, headers=self.auth_headers) as resp:
                if not resp.is_success:
                    resp.read()
                    raise BlockTransferError(block["index"], label, _error_detail(resp))
                with open(tmp_path, "r+b") as f:
                    f.seek(offset)
                    for chunk in resp.iter_bytes(chunk_size=CHUNK_SIZE):
                        written += len(chunk)
                        if written > block["size_bytes"]:
                            raise BlockTransferError(
                                block["index"], label, f"el bloque mide más de {block['size_bytes']} bytes"
                            )
                        f.write(chunk)
                        hasher.update(chunk)
                        if progress_cb:
                            progress_cb(len(chunk))
        except httpx.HTTPError as exc:
            raise BlockTransferError(block["index"], label, f"no se pudo conectar: {exc}") from exc

        if written != block["size_bytes"]:
            raise BlockTransferError(
                block["index"], label, f"se esperaban {block['size_bytes']} bytes y llegaron {written}"
            )
        expected = block.get("checksum")
        if expected and hasher.hexdigest() != expected:
            raise BlockTransferError(
                block["index"],
                label,
                f"checksum no coincide (esperado={expected}, obtenido={hasher.hexdigest()})",
            )

    # -- util --------------------------------------------------------------
    def _run_parallel(self, fn, items: list) -> list:
        if not items:
            return []
        workers = min(self.parallelism, len(items))
        if workers == 1:
            return [fn(item) for item in items]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # map conserva el orden y propaga la primera excepción.
            return list(pool.map(fn, items))
