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
DEFAULT_BASE_URL = os.getenv("DFSHA_BASE_URL", "http://localhost:8000")

# Tamaño de chunk para lectura/escritura en streaming de archivos (mismo
# criterio que el servidor, ver app/routers/transfer.py: CHUNK_SIZE).
CHUNK_SIZE = 64 * 1024

# Timeout por defecto para requests HTTP normales (no aplica a
# put/get de archivos grandes, donde se usa un timeout más permisivo).
DEFAULT_TIMEOUT = 10.0
UPLOAD_DOWNLOAD_TIMEOUT = 120.0
