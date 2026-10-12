#!/usr/bin/env python3
"""Prepara los secretos y certificados del clúster (Hito 3, RNF6).

Genera, una sola vez y FUERA del control de versiones:

- certs/ca.crt + ca.key: una autoridad certificadora propia del clúster.
- certs/<nodo>.crt + .key: un certificado TLS por nodo (controlnode-1/2,
  datanode-1..4), firmado por la CA, válido para el nombre del servicio en
  la red de docker-compose, `localhost` y 127.0.0.1.
- .env: secretos que lee docker-compose:
    DFSHA_JWT_SECRET       firma de los JWT de usuario
    DFSHA_CLUSTER_SECRET   tokens entre nodos y tokens de bloque
    DFSHA_DN<i>_STORAGE_KEY  clave AES-256 de cifrado en reposo de cada DataNode

Uso (requiere `pip install cryptography`):

    python scripts/setup_cluster.py            # no pisa lo que ya exista
    python scripts/setup_cluster.py --force    # regenera todo

La CLI verifica el TLS del clúster con certs/ca.crt:

    dfsha login ana -u https://localhost:8000,https://localhost:8010 --ca-cert certs/ca.crt
"""
from __future__ import annotations

import argparse
import base64
import ipaddress
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
except ImportError:  # pragma: no cover
    sys.exit("Falta la librería 'cryptography': pip install cryptography")

ROOT = Path(__file__).resolve().parent.parent
CERTS = ROOT / "certs"
ENV_FILE = ROOT / ".env"
NODES = ["controlnode-1", "controlnode-2", "datanode-1", "datanode-2", "datanode-3", "datanode-4"]
NUM_DATANODES = 4
VALID_DAYS = 825


def _key():
    return ec.generate_private_key(ec.SECP256R1())


def _write_key(key, path: Path) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    path.chmod(0o600)


def _write_cert(cert, path: Path) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def make_ca():
    key = _key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DFSha Cluster CA")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def make_node_cert(name: str, ca_key, ca_cert):
    key = _key()
    now = datetime.now(timezone.utc)
    san = x509.SubjectAlternativeName(
        [
            x509.DNSName(name),
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=VALID_DAYS))
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def write_certs(force: bool) -> None:
    CERTS.mkdir(exist_ok=True)
    if (CERTS / "ca.crt").exists() and not force:
        print(f"· {CERTS.relative_to(ROOT)}/ ya existe (usa --force para regenerar)")
        return
    ca_key, ca_cert = make_ca()
    _write_key(ca_key, CERTS / "ca.key")
    _write_cert(ca_cert, CERTS / "ca.crt")
    for name in NODES:
        key, cert = make_node_cert(name, ca_key, ca_cert)
        _write_key(key, CERTS / f"{name}.key")
        _write_cert(cert, CERTS / f"{name}.crt")
        # El contenedor corre con otro usuario: la clave debe poder leerse
        # desde el volumen montado en solo lectura.
        (CERTS / f"{name}.key").chmod(0o644)
    print(f"✓ CA y {len(NODES)} certificados en {CERTS.relative_to(ROOT)}/")


def write_env(force: bool) -> None:
    if ENV_FILE.exists() and not force:
        print(f"· {ENV_FILE.name} ya existe (usa --force para regenerar; ¡las claves de cifrado cambiarían!)")
        return
    lines = [
        "# Generado por scripts/setup_cluster.py. NO subir al repositorio.",
        f"DFSHA_JWT_SECRET={secrets.token_urlsafe(48)}",
        f"DFSHA_CLUSTER_SECRET={secrets.token_urlsafe(48)}",
    ]
    for i in range(1, NUM_DATANODES + 1):
        lines.append(f"DFSHA_DN{i}_STORAGE_KEY={base64.b64encode(os.urandom(32)).decode()}")
    ENV_FILE.write_text("\n".join(lines) + "\n")
    ENV_FILE.chmod(0o600)
    print(f"✓ Secretos en {ENV_FILE.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="Regenerar aunque ya existan")
    args = parser.parse_args()
    write_certs(args.force)
    write_env(args.force)


if __name__ == "__main__":
    main()
