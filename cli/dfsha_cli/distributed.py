"""
RF2 distribuido: put/get por bloques contra ControlNode + DataNodes.

El usuario sigue escribiendo `dfsha put archivo.txt` sin saber dónde caen
los bloques (transparencia de localización). Por debajo:

put (escritura en dos fases)
    1. POST {controlnode}/files/{ruta}/allocate con el tamaño del archivo
       -> plan: para cada bloque, su cadena de réplica (3 DataNodes por
       defecto), un token de escritura firmado y un lease exclusivo.
    2. Para cada bloque, EN PARALELO: se calcula su SHA-256 leyendo solo
       ese tramo del archivo y se sube en streaming al PRIMER DataNode de su
       cadena (PUT {datanode}/blocks/{block_id}?file_id=..., con el token y
       X-Checksum-Sha256). Ese DataNode lo guarda y lo reenvía al siguiente
       de la cadena (DataNode -> DataNode), y así hasta el último. Si el
       primero no responde, se prueba el siguiente.
    3. POST {controlnode}/files/{ruta}/confirm con checksums y tamaños. El
       ControlNode solo acepta bloques que los DataNodes le informaron haber
       guardado; si los informes aún no llegaron (409 replicas_not_ready) se
       reintenta unos segundos.
    Mientras dura la subida, el lease se renueva en segundo plano.

get
    1. GET {controlnode}/files/{ruta}/blocks -> plan de lectura: réplicas de
       cada bloque (vivas primero), checksum y token de lectura.
    2. Se crea el archivo destino con su tamaño final y cada bloque se
       descarga EN PARALELO, escribiéndose directamente en su offset
       mientras se calcula su checksum. Si una réplica falla (caída, no lo
       tiene, checksum distinto), se prueba la siguiente: así un DataNode
       caído no impide leer (alta disponibilidad).
    3. Si un bloque no se puede bajar de ninguna réplica, se aborta y no
       queda ningún archivo a medias (se escribe a un temporal que solo se
       renombra al final).

Memoria: nunca se carga un bloque entero; todo va en chunks de CHUNK_SIZE.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from dfsha_cli.config import CHUNK_SIZE, PARALLEL_TRANSFERS, UPLOAD_DOWNLOAD_TIMEOUT

ProgressCallback = Callable[[int], None]

# Reintentos de confirm mientras los DataNodes informan los bloques.
CONFIRM_RETRY_SECONDS = 20.0
# Cada cuánto se renueva el lease de escritura durante una subida larga.
LEASE_RENEW_SECONDS = 30.0


@dataclass
class BlockTransferError(Exception):
    """Falló la transferencia de un bloque con los DataNodes."""

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
    # node_id -> cuántos bloques (réplicas) quedaron en / salieron de ese nodo
    blocks_per_node: dict[str, int] = field(default_factory=dict)
    complete: bool = True
    replication: int = 1
    failovers: int = 0


def datanode_url(location: dict) -> str:
    return f"{location.get('scheme', 'http')}://{location['host']}:{location['port']}"


def _node_label(location: dict) -> str:
    return f"{location['node_id']} ({location['host']}:{location['port']})"


def _locations(block: dict) -> list[dict]:
    return block.get("datanodes") or [block["datanode"]]


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


def _block_auth(block: dict, fallback: dict) -> dict:
    token = block.get("token")
    return {"Authorization": f"Bearer {token}"} if token else dict(fallback)


class _LeaseRenewer:
    """Renueva el lease de escritura en segundo plano mientras dura la subida."""

    def __init__(self, control, path: str, lease_id: str | None) -> None:
        self._stop = threading.Event()
        self._thread = None
        if lease_id and hasattr(control, "renew_lock"):
            self._thread = threading.Thread(target=self._run, args=(control, path, lease_id), daemon=True)

    def _run(self, control, path: str, lease_id: str) -> None:
        while not self._stop.wait(LEASE_RENEW_SECONDS):
            try:
                control.renew_lock(path, lease_id)
            except Exception:  # noqa: BLE001 - si falla, confirm lo dirá
                pass

    def __enter__(self) -> "_LeaseRenewer":
        if self._thread:
            self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()


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
        self.http = http or httpx.Client(timeout=UPLOAD_DOWNLOAD_TIMEOUT, verify=getattr(control, "verify", True))
        self.auth_headers = dict(control.auth_headers)
        self._failovers = 0
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> "DistributedTransfer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _count_failover(self) -> None:
        with self._lock:
            self._failovers += 1

    # -- put ---------------------------------------------------------------
    def put(
        self,
        remote_path: str,
        local_path: Path,
        block_size: int | None = None,
        progress_cb: ProgressCallback | None = None,
        lease_id: str | None = None,
    ) -> TransferSummary:
        size = local_path.stat().st_size
        plan = self.control.allocate(remote_path, size, block_size, lease_id=lease_id)
        lease = plan.get("lease_id") or lease_id

        offsets: list[int] = []
        offset = 0
        for block in plan["blocks"]:
            offsets.append(offset)
            offset += block["size_bytes"]
        if offset != size:
            raise BlockTransferError(
                index=-1, node="controlnode", detail=f"el plan suma {offset} bytes y el archivo tiene {size}"
            )

        def upload(item: tuple[dict, int]) -> tuple[dict, list[str]]:
            block, block_offset = item
            return self._upload_block(local_path, block, block_offset, progress_cb)

        with _LeaseRenewer(self.control, remote_path, lease):
            results = self._run_parallel(upload, list(zip(plan["blocks"], offsets)))
            confirmations = [confirmation for confirmation, _ in results]
            confirmed = self._confirm(remote_path, confirmations, lease)

        placement = Counter(node for _, stored_on in results for node in stored_on)
        return TransferSummary(
            path=confirmed["path"],
            size_bytes=confirmed["size_bytes"],
            num_blocks=confirmed["num_blocks"],
            blocks_per_node=dict(placement),
            complete=confirmed["complete"],
            replication=plan.get("replication", 1),
            failovers=self._failovers,
        )

    def _confirm(self, remote_path: str, confirmations: list[dict], lease: str | None) -> dict:
        deadline = time.monotonic() + CONFIRM_RETRY_SECONDS
        delay = 0.2
        while True:
            try:
                return self.control.confirm(remote_path, confirmations, lease_id=lease)
            except Exception as exc:  # APIError del cliente: se mira el código
                if getattr(exc, "error", None) != "replicas_not_ready" or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 2.0)

    def _upload_block(
        self, local_path: Path, block: dict, offset: int, progress_cb: ProgressCallback | None
    ) -> tuple[dict, list[str]]:
        length = block["size_bytes"]
        checksum = _sha256_range(local_path, offset, length)
        headers = {**_block_auth(block, self.auth_headers), "X-Checksum-Sha256": checksum, "Content-Length": str(length)}

        errors: list[str] = []
        for attempt, location in enumerate(_locations(block)):
            if attempt:
                self._count_failover()

            def body() -> Iterator[bytes]:
                for chunk in _read_range(local_path, offset, length):
                    if progress_cb and not errors:
                        progress_cb(len(chunk))
                    yield chunk

            try:
                resp = self.http.put(
                    f"{datanode_url(location)}/blocks/{block['block_id']}",
                    params={"file_id": str(block["file_id"])},
                    content=body(),
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                errors.append(f"{_node_label(location)}: no se pudo conectar ({exc})")
                continue
            if not resp.is_success:
                errors.append(f"{_node_label(location)}: {_error_detail(resp)}")
                if resp.status_code in (400, 403):
                    break  # checksum o token: otro nodo no lo va a arreglar
                continue
            stored_on = resp.json().get("stored_on", [location["node_id"]])
            return {"block_id": block["block_id"], "checksum": checksum, "size_bytes": length}, stored_on
        raise BlockTransferError(block["index"], "todas las réplicas", "; ".join(errors))

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

        def download(item: tuple[dict, int]) -> str:
            block, block_offset = item
            return self._download_block(tmp_path, block, block_offset, progress_cb)

        try:
            sources = self._run_parallel(download, list(zip(blocks, offsets)))
            os.replace(tmp_path, local_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        return TransferSummary(
            path=plan["path"],
            size_bytes=total,
            num_blocks=len(blocks),
            blocks_per_node=dict(Counter(sources)),
            failovers=self._failovers,
        )

    def _download_block(
        self, tmp_path: Path, block: dict, offset: int, progress_cb: ProgressCallback | None
    ) -> str:
        """Baja un bloque de la primera réplica que funcione. Devuelve el node_id."""
        errors: list[str] = []
        headers = _block_auth(block, self.auth_headers)
        for attempt, location in enumerate(_locations(block)):
            if attempt:
                self._count_failover()
            try:
                self._download_from(tmp_path, block, offset, location, headers, progress_cb)
                return location["node_id"]
            except BlockTransferError as exc:
                errors.append(f"{_node_label(location)}: {exc.detail}")
        raise BlockTransferError(block["index"], "todas las réplicas", "; ".join(errors))

    def _download_from(
        self,
        tmp_path: Path,
        block: dict,
        offset: int,
        location: dict,
        headers: dict,
        progress_cb: ProgressCallback | None,
    ) -> None:
        label = _node_label(location)
        url = f"{datanode_url(location)}/blocks/{block['block_id']}"
        hasher = hashlib.sha256()
        written = 0
        try:
            with self.http.stream("GET", url, headers=headers) as resp:
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
