#!/usr/bin/env python3
"""Verificación end-to-end del DFS distribuido por bloques (Hito 2).

Contra un cluster YA LEVANTADO (ver README: `docker compose up --build`,
o el ControlNode + al menos 1 DataNode corriendo localmente), este script:

1. Hace login y crea un archivo de prueba con contenido aleatorio.
2. Pide el plan de escritura (`allocate`), sube cada bloque al DataNode
   que le tocó y confirma (`confirm`). Imprime en qué nodos quedó cada
   bloque, para que se vea a simple vista que el archivo SÍ quedó
   particionado entre varios DataNodes (RNF4).
3. Pide el plan de lectura (`blocks`), descarga cada bloque de SU
   DataNode, verifica su checksum y reensambla — compara contra el
   contenido original.
4. Reemplaza el bloque 0 vía el endpoint CRUD
   (`POST .../blocks/0/allocate`) y confirma que SOLO ese bloque cambió.

Uso:
    pip install httpx
    python scripts/verify_hito2.py [--base-url http://localhost:8000] [--size 200000] [--block-size 65536]

Sale con código 0 si todo pasó, 1 si algo falló.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

import httpx


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")


def fail(msg: str) -> None:
    print(f"  \033[31m✗\033[0m {msg}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--size", type=int, default=200_000, help="Tamaño del archivo de prueba (bytes)")
    parser.add_argument("--block-size", type=int, default=65_536, help="Tamaño de bloque a pedir")
    parser.add_argument("--path", default="/verify_hito2_test.bin")
    args = parser.parse_args()

    failures = 0
    client = httpx.Client(base_url=args.base_url, timeout=10.0)

    # --- 0. Login ----------------------------------------------------
    print("[0] Autenticación")
    resp = client.post("/auth/login", json={"username": "verify-script", "password": "verify-1234"})
    if resp.status_code != 200:
        fail(f"login falló: {resp.status_code} {resp.text}")
        return 1
    token = resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    ok("login OK, token obtenido")

    # --- 1. Datos de prueba -------------------------------------------
    original = os.urandom(args.size)
    original_checksum = sha256(original)

    # --- 2. Allocate + subir bloques + confirm -------------------------
    print(f"\n[1] Allocate de {args.path} ({args.size} bytes, block_size={args.block_size})")
    resp = client.post(
        f"/files{args.path}/allocate",
        json={"size_bytes": args.size, "block_size": args.block_size},
    )
    if resp.status_code != 200:
        fail(f"allocate falló: {resp.status_code} {resp.text}")
        return 1
    plan = resp.json()
    nodes_used = sorted({b["datanode"]["node_id"] for b in plan["blocks"]})
    ok(f"{len(plan['blocks'])} bloque(s) planificados, repartidos entre {len(nodes_used)} nodo(s): {nodes_used}")
    if len(plan["blocks"]) > 1 and len(nodes_used) == 1:
        fail("todos los bloques cayeron en el MISMO nodo — revisa el round-robin o cuántos DataNodes están vivos")
        failures += 1

    confirm_blocks = []
    for block in plan["blocks"]:
        start = block["index"] * plan["block_size"]
        data = original[start : start + block["size_bytes"]]
        dn = block["datanode"]
        put_url = f"http://{dn['host']}:{dn['port']}/blocks/{block['block_id']}"
        put_resp = httpx.put(put_url, params={"file_id": block["file_id"]}, content=data, timeout=10.0)
        if put_resp.status_code != 200:
            fail(f"PUT a datanode {dn['node_id']} falló: {put_resp.status_code} {put_resp.text}")
            return 1
        confirm_blocks.append(
            {"block_id": block["block_id"], "checksum": sha256(data), "size_bytes": len(data)}
        )
    ok(f"{len(confirm_blocks)} bloque(s) subidos directo a sus DataNodes")

    resp = client.post(f"/files{args.path}/confirm", json={"blocks": confirm_blocks})
    confirmed = resp.json()
    if not confirmed.get("complete"):
        fail(f"confirm incompleto: {confirmed}")
        failures += 1
    else:
        ok(f"confirm OK: {confirmed['num_blocks']} bloque(s), {confirmed['size_bytes']} bytes")

    # --- 3. Leer y verificar --------------------------------------------
    print("\n[2] Plan de lectura + descarga + verificación de checksum")
    resp = client.get(f"/files{args.path}/blocks")
    read_plan = resp.json()
    reassembled = bytearray()
    for block in read_plan["blocks"]:
        dn = block["datanode"]
        get_resp = httpx.get(f"http://{dn['host']}:{dn['port']}/blocks/{block['block_id']}", timeout=10.0)
        if get_resp.status_code != 200:
            fail(f"GET a datanode {dn['node_id']} falló: {get_resp.status_code}")
            failures += 1
            continue
        data = get_resp.content
        if sha256(data) != block["checksum"]:
            fail(f"checksum no coincide en bloque {block['index']} (nodo {dn['node_id']})")
            failures += 1
        reassembled.extend(data)

    if bytes(reassembled) == original and sha256(bytes(reassembled)) == original_checksum:
        ok("archivo reensamblado coincide byte a byte con el original")
    else:
        fail("el archivo reensamblado NO coincide con el original")
        failures += 1

    # --- 4. CRUD: reemplazar un bloque puntual ---------------------------
    print("\n[3] Reemplazo CRUD del bloque 0 (sin re-subir el resto)")
    old_checksums = {b["index"]: b["checksum"] for b in read_plan["blocks"]}
    new_content = os.urandom(min(4096, args.block_size))
    resp = client.post(f"/files{args.path}/blocks/0/allocate", json={"size_bytes": len(new_content)})
    if resp.status_code != 200:
        fail(f"allocate de bloque puntual falló: {resp.status_code} {resp.text}")
        failures += 1
    else:
        alloc = resp.json()
        dn = alloc["datanode"]
        httpx.put(
            f"http://{dn['host']}:{dn['port']}/blocks/{alloc['block_id']}",
            params={"file_id": alloc["file_id"]},
            content=new_content,
            timeout=10.0,
        )
        client.post(
            f"/files{args.path}/confirm",
            json={"blocks": [{"block_id": alloc["block_id"], "checksum": sha256(new_content), "size_bytes": len(new_content)}]},
        )
        after = client.get(f"/files{args.path}/blocks").json()
        after_by_index = {b["index"]: b["checksum"] for b in after["blocks"]}
        block0_changed = after_by_index.get(0) != old_checksums.get(0)
        others_unchanged = all(
            after_by_index.get(i) == old_checksums.get(i) for i in old_checksums if i != 0
        )
        if block0_changed and others_unchanged:
            ok("bloque 0 reemplazado y el resto de bloques quedó intacto (CRUD funcionando)")
        else:
            fail(f"CRUD parcial no se comportó como se esperaba (block0_changed={block0_changed}, others_unchanged={others_unchanged})")
            failures += 1

    # --- Limpieza ----------------------------------------------------
    client.request("DELETE", f"/fs/rm", json={"path": args.path})

    print()
    if failures == 0:
        print("\033[32mTODO OK — el particionamiento distribuido funciona end-to-end.\033[0m")
        return 0
    print(f"\033[31m{failures} verificación(es) fallaron.\033[0m")
    return 1


if __name__ == "__main__":
    sys.exit(main())
