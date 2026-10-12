"""
Configuración global del cliente CLI.

Centraliza rutas y valores por defecto para que el resto de módulos no
tengan que leer variables de entorno o construir paths por su cuenta.

Convenciones acordadas con el resto del equipo (ver CONTRATOS.md en la
raíz del repo del servidor):
- Base URL configurable, por defecto http://localhost:8000.
- La CLI siempre manda rutas absolutas al servidor (nunca cwd_id); el
  directorio de trabajo ("cd") se resuelve enteramente en el cliente,
  ver pathutils.py.
"""
from __future__ import annotations

import os
from pathlib import Path

# Directorio de configuración local del usuario (independiente del
# proyecto: vive en el home de quien use la CLI, no en el repo).
CONFIG_DIR = Path(os.getenv("DFSHA_CONFIG_DIR", str(Path.home() / ".dfsha")))
SESSION_FILE = CONFIG_DIR / "session.json"

# Base URL por defecto del servidor DFSha. Puede sobreescribirse:
# - con la variable de entorno DFSHA_BASE_URL, o
# - con el flag --base-url en `dfsha login`.
# Con alta disponibilidad (Hito 3) puede ser una LISTA separada por comas
# (un ControlNode líder y sus respaldos): la CLI usa el que responda como
# líder y cambia sola si hay un failover.
DEFAULT_BASE_URL = os.getenv("DFSHA_BASE_URL", "http://localhost:8000")

# CA con la que se verifican los certificados TLS del clúster (ControlNodes
# y DataNodes). Vacío = verificación por defecto del sistema.
DEFAULT_CA_CERT = os.getenv("DFSHA_CA_CERT", "")

# Tamaño de chunk para lectura/escritura en streaming de archivos (mismo
# criterio que el servidor, ver app/routers/transfer.py: CHUNK_SIZE).
CHUNK_SIZE = 64 * 1024

# Timeout por defecto para requests HTTP normales (no aplica a
# put/get de archivos grandes, donde se usa un timeout más permisivo).
DEFAULT_TIMEOUT = 10.0
UPLOAD_DOWNLOAD_TIMEOUT = 120.0

# --- Hito 2: transferencia por bloques -----------------------------------
# Cuántos bloques se suben/bajan a la vez contra los DataNodes.
PARALLEL_TRANSFERS = int(os.getenv("DFSHA_PARALLEL_TRANSFERS", "4"))

# Tamaño de bloque que pide `put` al ControlNode. Vacío = el que tenga
# configurado el ControlNode (8 MiB por defecto). Útil para la demo: con
# DFSHA_BLOCK_SIZE=65536 un archivo pequeño ya se reparte en varios nodos.
DEFAULT_BLOCK_SIZE = int(os.getenv("DFSHA_BLOCK_SIZE")) if os.getenv("DFSHA_BLOCK_SIZE") else None
