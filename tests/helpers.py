"""Ayudas para los tests del ControlNode: hablar como un DataNode (con token
de nodo firmado con el secreto del clúster) y simular que los DataNodes
guardaron los bloques de un plan."""
import hashlib

from app.cluster_auth import node_auth_headers


def cs(label) -> str:
    """Checksum SHA-256 (hex) determinista para usar en los tests."""
    return hashlib.sha256(str(label).encode()).hexdigest()


def node_headers(node_id: str) -> dict[str, str]:
    return node_auth_headers(node_id, "datanode")


def register(client, node_id, host="10.0.0.1", port=9000, **extra):
    return client.post(
        "/datanodes/register",
        json={"node_id": node_id, "host": host, "port": port, **extra},
        headers=node_headers(node_id),
    )


def register_n(client, n):
    """Registra n DataNodes vivos: dn-0 .. dn-(n-1)."""
    for i in range(n):
        register(client, f"dn-{i}", host=f"10.0.0.{i + 1}", port=9000 + i)


def heartbeat(client, node_id, deleted=(), **extra):
    return client.post(
        "/datanodes/heartbeat",
        json={"node_id": node_id, "deleted_blocks": list(deleted), **extra},
        headers=node_headers(node_id),
    )


def report_stored(client, node_id, block_id, checksum, size):
    resp = client.post(
        "/datanodes/blocks/received",
        json={"node_id": node_id, "blocks": [{"block_id": block_id, "checksum": checksum, "size_bytes": size}]},
        headers=node_headers(node_id),
    )
    assert resp.status_code == 204, resp.text
    return resp


def store_block(client, block, checksum=None, nodes=None):
    """Simula que los DataNodes de la cadena (o `nodes`) guardaron el bloque.
    Devuelve la entrada de confirm correspondiente."""
    checksum = checksum or cs(block["block_id"])
    targets = nodes if nodes is not None else [n["node_id"] for n in block["datanodes"]]
    for node_id in targets:
        report_stored(client, node_id, block["block_id"], checksum, block["size_bytes"])
    return {"block_id": block["block_id"], "checksum": checksum, "size_bytes": block["size_bytes"]}


def write_file(client, path, size, block_size=10, checksums=None):
    """allocate + (los DataNodes guardan) + confirm. Devuelve el plan."""
    plan = client.post(f"/files{path}/allocate", json={"size_bytes": size, "block_size": block_size}).json()
    confirmations = [
        store_block(client, b, checksum=(checksums[b["index"]] if checksums else None)) for b in plan["blocks"]
    ]
    resp = client.post(f"/files{path}/confirm", json={"blocks": confirmations, "lease_id": plan["lease_id"]})
    assert resp.status_code == 200, resp.text
    return plan


def deletions_for(client, node_id, deleted=()):
    resp = heartbeat(client, node_id, deleted)
    assert resp.status_code == 200, resp.text
    return set(resp.json()["delete_blocks"])
