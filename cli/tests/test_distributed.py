"""Tests de put/get distribuido (dfsha_cli/distributed.py) contra un
ControlNode y unos DataNodes falsos en memoria, con replicación."""
import hashlib
import threading

import httpx
import pytest

from dfsha_cli.client import APIError
from dfsha_cli.distributed import BlockTransferError, DistributedTransfer

SESSION_HEADERS = {"Authorization": "Bearer jwt-de-sesion"}


class FakeCluster:
    """ControlNode (allocate/confirm/blocks, como DFShaClient) y DataNodes
    (MockTransport). Cada bloque se asigna a una cadena de `replication`
    nodos en anillo; el DataNode que recibe un PUT "reenvía" (en memoria) a
    los que le siguen en la cadena, como hace el DataNode real."""

    def __init__(self, nodes=("dn-1", "dn-2", "dn-3"), default_block_size=8, replication=2):
        self.nodes = list(nodes)
        self.default_block_size = default_block_size
        self.replication = replication
        self.auth_headers = dict(SESSION_HEADERS)
        self.files: dict[str, list[dict]] = {}
        self.pending: dict[str, list[dict]] = {}
        self.storage: dict[str, dict[str, bytes]] = {n: {} for n in nodes}
        self.down: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.lock = threading.Lock()
        self.confirm_failures = 0
        self.confirm_calls = 0
        self.leases: list[str | None] = []
        self._next_id = 0

    # --- ControlNode ---
    def _loc(self, node):
        return {"node_id": node, "host": node, "port": 9000, "scheme": "http"}

    def allocate(self, path, size, block_size=None, lease_id=None):
        bs = block_size or self.default_block_size
        blocks, offset, i = [], 0, 0
        while offset < size:
            length = min(bs, size - offset)
            chain = [self.nodes[(i + k) % len(self.nodes)] for k in range(self.replication)]
            self._next_id += 1
            bid = f"blk{self._next_id}"
            blocks.append(
                {
                    "index": i,
                    "block_id": bid,
                    "file_id": 42,
                    "size_bytes": length,
                    "datanode": self._loc(chain[0]),
                    "datanodes": [self._loc(n) for n in chain],
                    "token": f"write-{bid}",
                }
            )
            offset += length
            i += 1
        self.pending[path] = blocks
        return {
            "path": path,
            "file_id": 42,
            "block_size": bs,
            "total_size": size,
            "blocks": blocks,
            "lease_id": "lease-1",
            "replication": self.replication,
        }

    def confirm(self, path, confirmations, lease_id=None):
        self.confirm_calls += 1
        self.leases.append(lease_id)
        if self.confirm_failures > 0:
            self.confirm_failures -= 1
            raise APIError(status_code=409, error="replicas_not_ready", message="aún no")
        plan = {b["block_id"]: b for b in self.pending[path]}
        committed = []
        for c in confirmations:
            block = dict(plan[c["block_id"]])
            block["checksum"] = c["checksum"]
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
        blocks = [
            {**b, "token": f"read-{b['block_id']}"} for b in self.files[path]
        ]
        return {"path": path, "file_id": 42, "size_bytes": sum(b["size_bytes"] for b in blocks), "num_blocks": len(blocks), "blocks": blocks}

    def _chain_after(self, block_id, node):
        for blocks in [*self.pending.values(), *self.files.values()]:
            for b in blocks:
                if b["block_id"] == block_id:
                    chain = [n["node_id"] for n in b["datanodes"]]
                    return chain[chain.index(node) + 1 :]
        return []

    # --- DataNodes ---
    def handler(self, request: httpx.Request) -> httpx.Response:
        node = request.url.host
        with self.lock:
            self.requests.append(request)
        if node in self.down:
            raise httpx.ConnectError("caído", request=request)
        block_id = request.url.path.rsplit("/", 1)[-1]
        token = request.headers.get("authorization", "")
        if request.method == "PUT":
            if token != f"Bearer write-{block_id}":
                return httpx.Response(401, json={"detail": {"error": "invalid_token", "message": "x"}})
            data = request.read()
            if hashlib.sha256(data).hexdigest() != request.headers["x-checksum-sha256"]:
                return httpx.Response(400, json={"detail": {"error": "checksum_mismatch", "message": "x"}})
            stored = [node]
            with self.lock:
                self.storage[node][block_id] = data
                for nxt in self._chain_after(block_id, node):
                    if nxt not in self.down:
                        self.storage[nxt][block_id] = data
                        stored.append(nxt)
            return httpx.Response(200, json={"block_id": block_id, "stored_on": stored, "failed": []})
        if request.method == "GET":
            if token != f"Bearer read-{block_id}":
                return httpx.Response(401, json={"detail": {"error": "invalid_token", "message": "x"}})
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


def test_put_parte_el_archivo_y_cada_bloque_queda_replicado(cluster, transfer, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(bytes(range(20)))  # 3 bloques de 8, 8, 4

    summary = transfer.put("/a.bin", src)

    assert summary.num_blocks == 3
    assert summary.size_bytes == 20
    assert summary.complete
    assert summary.replication == 2
    # Cada bloque en 2 nodos distintos; ningún nodo tiene el archivo completo.
    assert summary.blocks_per_node == {"dn-1": 2, "dn-2": 2, "dn-3": 2}
    assert cluster.storage["dn-1"]["blk1"] == bytes(range(0, 8))
    assert cluster.storage["dn-2"]["blk1"] == bytes(range(0, 8))
    assert "blk2" not in cluster.storage["dn-1"]


def test_el_cliente_sube_una_sola_vez_por_bloque(cluster, transfer, tmp_path):
    """La réplica la hace la cadena de DataNodes, no el cliente."""
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 20)
    transfer.put("/a.bin", src)
    puts = [r for r in cluster.requests if r.method == "PUT"]
    assert len(puts) == 3
    assert [r.url.host for r in puts] == ["dn-1", "dn-2", "dn-3"]


def test_put_usa_el_token_de_bloque_y_manda_el_lease_en_confirm(cluster, transfer, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 10)
    transfer.put("/a.bin", src)
    for r in (r for r in cluster.requests if r.method == "PUT"):
        assert r.headers["authorization"].startswith("Bearer write-")
        assert r.url.params["file_id"] == "42"
    assert cluster.leases == ["lease-1"]


def test_si_el_primario_esta_caido_sube_al_siguiente_de_la_cadena(cluster, transfer, tmp_path):
    cluster.down.add("dn-1")
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 8)  # un bloque: cadena dn-1, dn-2
    summary = transfer.put("/a.bin", src)
    assert summary.blocks_per_node == {"dn-2": 1}
    assert summary.failovers == 1


def test_confirm_se_reintenta_mientras_los_datanodes_informan(cluster, transfer, tmp_path):
    cluster.confirm_failures = 2
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 8)
    assert transfer.put("/a.bin", src).complete
    assert cluster.confirm_calls == 3


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


def test_get_usa_el_token_de_lectura(cluster, transfer, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"0123456789")
    transfer.put("/a.bin", src)
    transfer.get("/a.bin", tmp_path / "out.bin")
    gets = [r for r in cluster.requests if r.method == "GET"]
    assert gets and all(r.headers["authorization"].startswith("Bearer read-") for r in gets)


def test_get_con_un_datanode_caido_lee_de_otra_replica(cluster, transfer, tmp_path):
    """Alta disponibilidad: el archivo se sigue pudiendo leer."""
    src = tmp_path / "a.bin"
    content = bytes(range(24))
    src.write_bytes(content)
    transfer.put("/a.bin", src)
    cluster.down.add("dn-2")

    dest = tmp_path / "out.bin"
    summary = transfer.get("/a.bin", dest)
    assert dest.read_bytes() == content
    assert "dn-2" not in summary.blocks_per_node
    assert summary.failovers >= 1


def test_archivo_vacio(cluster, transfer, tmp_path):
    src = tmp_path / "vacio.txt"
    src.write_bytes(b"")
    summary = transfer.put("/vacio.txt", src)
    assert summary.num_blocks == 0 and summary.complete
    dest = tmp_path / "out.txt"
    transfer.get("/vacio.txt", dest)
    assert dest.read_bytes() == b""


def test_replica_corrupta_se_salta_y_se_usa_otra(cluster, transfer, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"0123456789abcdef!!")
    transfer.put("/a.bin", src)
    cluster.storage["dn-2"]["blk2"] = b"XXXXXXXX"  # primera réplica del bloque 1, alterada
    dest = tmp_path / "out.bin"
    transfer.get("/a.bin", dest)
    assert dest.read_bytes() == b"0123456789abcdef!!"


def test_get_falla_si_ninguna_replica_sirve_y_no_deja_archivo(cluster, transfer, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"0123456789abcdef!!")
    transfer.put("/a.bin", src)
    cluster.storage["dn-2"]["blk2"] = b"XXXXXXXX"
    cluster.storage["dn-3"]["blk2"] = b"YYYYYYYY"

    dest = tmp_path / "out.bin"
    with pytest.raises(BlockTransferError) as info:
        transfer.get("/a.bin", dest)
    assert info.value.index == 1
    assert "checksum" in info.value.detail
    assert not dest.exists()
    assert not list(tmp_path.glob(".*dfsha-part"))


def test_get_no_pisa_un_archivo_local_existente_si_falla(cluster, transfer, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"0123456789")
    transfer.put("/a.bin", src)
    for node in cluster.nodes:
        cluster.storage[node].pop("blk2", None)

    dest = tmp_path / "previo.bin"
    dest.write_bytes(b"lo que ya tenia")
    with pytest.raises(BlockTransferError) as info:
        transfer.get("/a.bin", dest)
    assert "not_found" in info.value.detail
    assert dest.read_bytes() == b"lo que ya tenia"


def test_put_falla_si_ningun_nodo_de_la_cadena_responde(cluster, transfer, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 8)
    cluster.down.update({"dn-1", "dn-2"})
    with pytest.raises(BlockTransferError) as info:
        transfer.put("/a.bin", src)
    assert "no se pudo conectar" in str(info.value)
    assert "/a.bin" not in cluster.files  # no se llegó a confirmar
