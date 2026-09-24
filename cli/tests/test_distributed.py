"""Tests de put/get distribuido (dfsha_cli/distributed.py) contra un
ControlNode y unos DataNodes falsos en memoria."""
import hashlib
import threading

import httpx
import pytest

from dfsha_cli.distributed import BlockTransferError, DistributedTransfer

TOKEN_HEADERS = {"Authorization": "Bearer jwt-de-prueba"}


class FakeCluster:
    """ControlNode (métodos allocate/confirm/blocks, como DFShaClient) y
    DataNodes (MockTransport) con round-robin sobre `nodes`."""

    def __init__(self, nodes=("dn-1", "dn-2", "dn-3"), default_block_size=8):
        self.nodes = list(nodes)
        self.default_block_size = default_block_size
        self.auth_headers = dict(TOKEN_HEADERS)
        self.files: dict[str, list[dict]] = {}
        self.pending: dict[str, list[dict]] = {}
        self.storage: dict[str, dict[str, bytes]] = {n: {} for n in nodes}
        self.requests: list[httpx.Request] = []
        self.lock = threading.Lock()
        self.fail_put_on: str | None = None
        self._next_id = 0

    # --- ControlNode ---
    def allocate(self, path, size, block_size=None):
        bs = block_size or self.default_block_size
        blocks, offset, i = [], 0, 0
        while offset < size:
            length = min(bs, size - offset)
            node = self.nodes[i % len(self.nodes)]
            self._next_id += 1
            blocks.append(
                {
                    "index": i,
                    "block_id": f"blk{self._next_id}",
                    "file_id": 42,
                    "size_bytes": length,
                    "datanode": {"node_id": node, "host": node, "port": 9000},
                }
            )
            offset += length
            i += 1
        self.pending[path] = blocks
        return {"path": path, "file_id": 42, "block_size": bs, "total_size": size, "blocks": blocks}

    def confirm(self, path, confirmations):
        plan = {b["block_id"]: b for b in self.pending[path]}
        committed = []
        for c in confirmations:
            block = dict(plan[c["block_id"]])
            block["checksum"] = c["checksum"]
            block["size_bytes"] = c["size_bytes"]
            committed.append(block)
        self.files[path] = sorted(committed, key=lambda b: b["index"])
        return {
            "path": path,
            "file_id": 42,
            "size_bytes": sum(b["size_bytes"] for b in committed),
            "num_blocks": len(committed),
            "complete": len(committed) == len(plan),
        }

    def blocks(self, path):
        blocks = self.files[path]
        return {
            "path": path,
            "file_id": 42,
            "size_bytes": sum(b["size_bytes"] for b in blocks),
            "num_blocks": len(blocks),
            "blocks": blocks,
        }

    # --- DataNodes ---
    def handler(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
        node = request.url.host
        block_id = request.url.path.rsplit("/", 1)[-1]
        if request.headers.get("authorization") != TOKEN_HEADERS["Authorization"]:
            return httpx.Response(401, json={"detail": {"error": "unauthenticated", "message": "sin token"}})
        if request.method == "PUT":
            data = request.read()
            if block_id == self.fail_put_on:
                return httpx.Response(503, json={"detail": {"error": "down", "message": "nodo caído"}})
            if hashlib.sha256(data).hexdigest() != request.headers["x-checksum-sha256"]:
                return httpx.Response(400, json={"detail": {"error": "checksum_mismatch", "message": "x"}})
            with self.lock:
                self.storage[node][block_id] = data
            return httpx.Response(200, json={"block_id": block_id})
        if request.method == "GET":
            data = self.storage[node].get(block_id)
            if data is None:
                return httpx.Response(404, json={"detail": {"error": "not_found", "message": block_id}})
            return httpx.Response(200, content=data)
        return httpx.Response(405)


@pytest.fixture()
def cluster():
    return FakeCluster()


@pytest.fixture()
def transfer(cluster):
    http = httpx.Client(transport=httpx.MockTransport(cluster.handler))
    with DistributedTransfer(cluster, http=http, parallelism=3) as t:
        yield t
    http.close()


def test_put_parte_el_archivo_y_reparte_los_bloques(cluster, transfer, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(bytes(range(20)))  # 3 bloques de 8, 8, 4

    summary = transfer.put("/a.bin", src)

    assert summary.num_blocks == 3
    assert summary.size_bytes == 20
    assert summary.complete
    assert summary.blocks_per_node == {"dn-1": 1, "dn-2": 1, "dn-3": 1}
    # Ningún DataNode tiene el archivo completo, solo su bloque.
    assert cluster.storage["dn-1"] == {"blk1": bytes(range(0, 8))}
    assert cluster.storage["dn-2"] == {"blk2": bytes(range(8, 16))}
    assert cluster.storage["dn-3"] == {"blk3": bytes(range(16, 20))}


def test_put_propaga_el_jwt_y_el_file_id_a_los_datanodes(cluster, transfer, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 10)
    transfer.put("/a.bin", src)
    puts = [r for r in cluster.requests if r.method == "PUT"]
    assert len(puts) == 2
    for r in puts:
        assert r.headers["authorization"] == "Bearer jwt-de-prueba"
        assert r.url.params["file_id"] == "42"


def test_put_y_get_ida_y_vuelta_con_archivo_grande(cluster, transfer, tmp_path):
    cluster.default_block_size = 50_000
    src = tmp_path / "grande.bin"
    content = bytes((i * 31) % 251 for i in range(333_333))
    src.write_bytes(content)

    transfer.put("/grande.bin", src)
    dest = tmp_path / "bajado" / "grande.bin"
    summary = transfer.get("/grande.bin", dest)

    assert dest.read_bytes() == content
    assert summary.num_blocks == 7
    assert not list(dest.parent.glob(".*dfsha-part"))


def test_archivo_vacio(cluster, transfer, tmp_path):
    src = tmp_path / "vacio.txt"
    src.write_bytes(b"")
    summary = transfer.put("/vacio.txt", src)
    assert summary.num_blocks == 0 and summary.complete
    dest = tmp_path / "out.txt"
    transfer.get("/vacio.txt", dest)
    assert dest.read_bytes() == b""


def test_get_detecta_bloque_corrupto_y_no_deja_archivo(cluster, transfer, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"0123456789abcdef!!")
    transfer.put("/a.bin", src)
    cluster.storage["dn-2"]["blk2"] = b"XXXXXXXX"  # mismo tamaño, otro contenido

    dest = tmp_path / "out.bin"
    with pytest.raises(BlockTransferError) as info:
        transfer.get("/a.bin", dest)
    assert info.value.index == 1
    assert "checksum" in info.value.detail
    assert not dest.exists()
    assert not list(tmp_path.glob(".*dfsha-part"))


def test_get_detecta_bloque_de_tamano_distinto(cluster, transfer, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"0123456789")
    transfer.put("/a.bin", src)
    cluster.storage["dn-1"]["blk1"] = b"corto"
    with pytest.raises(BlockTransferError):
        transfer.get("/a.bin", tmp_path / "out.bin")


def test_get_no_pisa_un_archivo_local_existente_si_falla(cluster, transfer, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"0123456789")
    transfer.put("/a.bin", src)
    cluster.storage["dn-2"].clear()  # el bloque 1 desapareció

    dest = tmp_path / "previo.bin"
    dest.write_bytes(b"lo que ya tenia")
    with pytest.raises(BlockTransferError) as info:
        transfer.get("/a.bin", dest)
    assert "not_found" in info.value.detail
    assert dest.read_bytes() == b"lo que ya tenia"


def test_put_falla_si_un_datanode_rechaza_y_no_confirma(cluster, transfer, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 20)
    cluster.fail_put_on = "blk2"
    with pytest.raises(BlockTransferError) as info:
        transfer.put("/a.bin", src)
    assert "dn-2" in str(info.value)
    assert "/a.bin" not in cluster.files  # no se llegó a confirmar


def test_datanode_inalcanzable_da_error_legible(cluster, tmp_path):
    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    http = httpx.Client(transport=httpx.MockTransport(boom))
    src = tmp_path / "a.bin"
    src.write_bytes(b"x")
    with DistributedTransfer(cluster, http=http) as transfer:
        with pytest.raises(BlockTransferError) as info:
            transfer.put("/a.bin", src)
    assert "no se pudo conectar" in str(info.value)
