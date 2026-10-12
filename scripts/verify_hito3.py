#!/usr/bin/env python3
"""Verificación end-to-end del Hito 3 contra el clúster de docker-compose.

Recorre, con la CLI real (`dfsha`) y caídas inyectadas con
`docker compose stop/start`:

1. Replicación: un archivo se parte en bloques y cada uno queda en 3
   DataNodes (cadena DataNode -> DataNode).
2. Seguridad: TLS obligatorio, otro usuario no puede leer (permisos), el
   DataNode rechaza peticiones sin token de bloque y los bloques están
   cifrados en disco.
3. Consistencia: con un lock exclusivo, otro usuario no puede escribir.
4. Alta disponibilidad de datos: se cae un DataNode, el archivo se sigue
   leyendo, el ControlNode re-replica sus bloques y, cuando vuelve, quita
   las réplicas sobrantes.
5. Alta disponibilidad del ControlNode: se cae el líder, el respaldo toma el
   control, la CLI sigue funcionando (lee y escribe) y el líder viejo vuelve
   como respaldo.

Requisitos: el clúster levantado (`docker compose up --build -d`), la CLI
instalada (`pip install -e ./cli`) y httpx. Tarda ~3 minutos (espera a que
venza el TTL de los heartbeats y al failover).

    python scripts/verify_hito3.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CA = ROOT / "certs" / "ca.crt"
URLS = "https://localhost:8000,https://localhost:8010"

failures = 0


def ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")


def fail(msg: str) -> None:
    global failures
    failures += 1
    print(f"  \033[31m✗\033[0m {msg}")


def check(condition: bool, good: str, bad: str) -> bool:
    (ok if condition else fail)(good if condition else bad)
    return condition


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


class User:
    """La CLI con una sesión aislada (su propio DFSHA_CONFIG_DIR)."""

    def __init__(self, name: str, password: str, workdir: Path) -> None:
        self.name = name
        self.env = {**os.environ, "DFSHA_CONFIG_DIR": str(workdir / f"session-{name}"), "COLUMNS": "120"}
        self.workdir = workdir
        result = self.run("login", name, "-u", URLS, "--ca-cert", str(CA), stdin=f"{password}\n")
        if result.returncode != 0:
            sys.exit(f"No se pudo iniciar sesión como {name}: {result.stderr or result.stdout}")

    def run(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["dfsha", *args], input=stdin, capture_output=True, text=True, env=self.env, cwd=self.workdir
        )

    def session(self) -> dict:
        return json.loads((Path(self.env["DFSHA_CONFIG_DIR"]) / "session.json").read_text())

    def api(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.session()['token']}"}
        for base in URLS.split(","):
            try:
                resp = httpx.request(method, f"{base}{path}", headers=headers, verify=str(CA), timeout=10, **kwargs)
            except httpx.HTTPError:
                continue
            if resp.status_code != 503:
                return resp
        raise RuntimeError("Ningún ControlNode respondió como líder")


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True, check=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wait_for(description: str, predicate, timeout: float, interval: float = 2.0) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            if predicate():
                ok(f"{description} ({time.monotonic() - start:.0f} s)")
                return True
        except Exception:  # noqa: BLE001 - mientras el clúster se recompone
            pass
        time.sleep(interval)
    fail(f"{description}: no ocurrió en {timeout:.0f} s")
    return False


def total_replicas(status: dict) -> int:
    return sum(node["blocks"] for node in status["datanodes"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--size-mb", type=int, default=8)
    parser.add_argument("--block-size", type=int, default=1024 * 1024)
    args = parser.parse_args()

    if not CA.exists():
        sys.exit("Falta certs/ca.crt: corre python scripts/setup_cluster.py y levanta el clúster")

    workdir = Path(tempfile.mkdtemp(prefix="dfsha-hito3-"))
    run_id = hashlib.sha256(os.urandom(8)).hexdigest()[:6]
    original = workdir / "datos.bin"
    marker = b"MARCADOR-EN-CLARO-" + run_id.encode()
    original.write_bytes(marker * 1000 + os.urandom(args.size_mb * 1024 * 1024))
    remote = f"/verify-{run_id}.bin"

    section("[0] Sesiones (TLS verificado con la CA del clúster)")
    ana = User(f"ana{run_id}", "clave-ana", workdir)
    bob = User(f"bob{run_id}", "clave-bob", workdir)
    ok(f"login de {ana.name} y {bob.name} contra {URLS}")

    section("[1] Replicación de bloques")
    result = ana.run("put", "-b", str(args.block_size), str(original), remote)
    check(result.returncode == 0, "put por la cadena de réplica", f"put falló: {result.stderr}")
    status = ana.api("GET", "/cluster/status").json()
    blocks = status["blocks"]
    factor = status["replication_factor"]
    check(
        total_replicas(status) == blocks * factor and status["under_replicated"] == 0,
        f"{blocks} bloques × {factor} réplicas = {total_replicas(status)} réplicas, ninguno sub-replicado",
        f"réplicas: {total_replicas(status)} para {blocks} bloques (sub-replicados {status['under_replicated']})",
    )
    nodes_used = [n["node_id"] for n in status["datanodes"] if n["blocks"]]
    check(len(nodes_used) == 4, f"repartidos entre {nodes_used}", f"solo en {nodes_used}")

    section("[2] Seguridad")
    try:
        httpx.get("http://localhost:8000/health", timeout=3)
        fail("el ControlNode acepta HTTP plano")
    except httpx.HTTPError:
        ok("HTTP plano rechazado: solo TLS")
    try:
        httpx.get("https://localhost:8000/health", timeout=3)
        fail("se aceptó el certificado sin la CA del clúster")
    except httpx.HTTPError:
        ok("certificado del clúster no confiable sin la CA (no hay MITM sin la CA)")
    result = bob.run("get", remote, str(workdir / "robado.bin"))
    check(
        result.returncode != 0 and "permission_denied" in result.stdout + result.stderr,
        f"{bob.name} no puede leer el archivo privado de {ana.name} (403)",
        f"{bob.name} pudo leer: {result.stdout}",
    )
    plan = ana.api("GET", f"/files{remote}/blocks").json()
    dn = plan["blocks"][0]["datanodes"][0]
    url = f"{dn['scheme']}://{dn['host']}:{dn['port']}/blocks/{plan['blocks'][0]['block_id']}"
    check(
        httpx.get(url, verify=str(CA)).status_code == 401,
        "DataNode sin token de bloque -> 401",
        "el DataNode sirvió un bloque sin token",
    )
    check(
        httpx.get(url, verify=str(CA), headers={"Authorization": f"Bearer {plan['blocks'][1]['token']}"}).status_code == 403,
        "token de otro bloque -> 403",
        "el DataNode aceptó el token de otro bloque",
    )
    found = subprocess.run(
        ["docker", "compose", "exec", "-T", "datanode-1", "grep", "-rl", marker.decode(), "/data/blocks"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    check(found.stdout.strip() == "", "bloques cifrados en disco (el contenido no aparece en claro)", f"texto en claro en {found.stdout}")

    section("[3] Consistencia: lock exclusivo")
    ana.run("chmod", "666", remote)
    lock = ana.run("lock", remote)
    lock_id = lock.stdout.split()[3] if lock.returncode == 0 else ""
    result = bob.run("put", str(original), remote)
    check(
        result.returncode != 0 and "file_locked" in result.stdout + result.stderr,
        f"con el lock de {ana.name}, {bob.name} no puede escribir (409 file_locked)",
        f"{bob.name} escribió con el archivo bloqueado: {result.stdout}",
    )
    ana.run("unlock", remote, lock_id)
    ana.run("chmod", "600", remote)

    section("[4] Alta disponibilidad de datos: cae datanode-2")
    compose("stop", "datanode-2")
    copy = workdir / "copia-sin-dn2.bin"
    result = ana.run("get", remote, str(copy))
    check(result.returncode == 0 and sha256(copy) == sha256(original), "lectura idéntica con datanode-2 caído", f"get falló: {result.stdout}{result.stderr}")
    wait_for(
        "el líder re-replicó los bloques de datanode-2 en los otros 3 nodos",
        lambda: (s := ana.api("GET", "/cluster/status").json())["under_replicated"] == 0
        and any(n["node_id"] == "datanode-2" and n["status"] == "dead" for n in s["datanodes"]),
        timeout=120,
    )
    compose("start", "datanode-2")
    wait_for(
        "datanode-2 volvió y se quitaron las réplicas sobrantes",
        lambda: (s := ana.api("GET", "/cluster/status").json())["under_replicated"] == 0
        and all(n["status"] == "alive" for n in s["datanodes"])
        and total_replicas(s) == s["blocks"] * factor,
        timeout=120,
    )

    section("[5] Alta disponibilidad del ControlNode: cae el líder")
    compose("stop", "controlnode-1")
    wait_for(
        "controlnode-2 tomó el control",
        lambda: httpx.get("https://localhost:8010/ha/status", verify=str(CA)).json()["role"] == "leader",
        timeout=90,
    )
    copy = workdir / "copia-tras-failover.bin"
    result = ana.run("get", remote, str(copy))
    check(result.returncode == 0 and sha256(copy) == sha256(original), "la CLI lee tras el failover (sin reconfigurar nada)", f"get falló: {result.stderr}")
    small = workdir / "nuevo.txt"
    small.write_text("escrito con el respaldo como líder\n")
    result = ana.run("put", str(small), f"/nuevo-{run_id}.txt")
    check(result.returncode == 0, "la CLI escribe tras el failover", f"put falló: {result.stderr}")
    compose("start", "controlnode-1")
    wait_for(
        "controlnode-1 volvió como respaldo del nuevo líder",
        lambda: (st := httpx.get("https://localhost:8000/ha/status", verify=str(CA)).json())["role"] == "standby"
        and st["leader_id"] == "cn2",
        timeout=90,
    )
    wait_for(
        "controlnode-1 tiene el archivo escrito mientras estaba caído",
        lambda: httpx.get("https://localhost:8000/ha/status", verify=str(CA)).json()["applied_seq"]
        >= httpx.get("https://localhost:8010/ha/status", verify=str(CA)).json()["applied_seq"] - 5,
        timeout=60,
    )

    section("Limpieza")
    ana.run("rm", remote)
    ana.run("rm", f"/nuevo-{run_id}.txt")
    ok("archivos de prueba borrados (sus bloques se borran tras el periodo de gracia)")

    print()
    if failures == 0:
        print("\033[32mTODO OK — replicación, seguridad, consistencia y alta disponibilidad funcionan.\033[0m")
        print("Nota: tras la prueba, controlnode-2 queda como líder; es lo esperado.")
        return 0
    print(f"\033[31m{failures} verificación(es) fallaron.\033[0m")
    return 1


if __name__ == "__main__":
    sys.exit(main())
