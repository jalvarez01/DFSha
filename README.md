# DFSha

Sistema de archivos distribuido por bloques, estilo HDFS.
**Hito 1**: versión monolítica cliente/servidor.

Stack: Python 3.11, FastAPI, SQLite (SQLAlchemy), Pydantic.

## Alcance de este repo en el Hito 1

El Hito 1 está completo: **servidor core y modelo de datos** (Juan),
**RF1 — gestión del sistema de archivos** (Jacobo), **RF2 —
transferencia de archivos** (Paulina) y la **CLI** (Mariana). Las firmas
exactas de cada endpoint están en [`CONTRATOS.md`](./CONTRATOS.md).

Queda fuera del Hito 1: la arquitectura distribuida
(Hito 2) y la alta disponibilidad, replicación y seguridad real —
autenticación con contraseña y control de acceso por usuario — (Hito 3).
Hoy el header `X-Username` identifica al dueño de cada operación pero no
restringe nada: el namespace es global.

## Estructura del repo

```
DFSha/
├── app/
│   ├── main.py            # App FastAPI, /health, arranque, routers
│   ├── config.py          # Settings (env vars)
│   ├── database.py        # engine, SessionLocal, get_db
│   ├── models.py          # Modelos SQLAlchemy: User, Directory, File
│   ├── path_service.py    # Resolución/validación de rutas (núcleo)
│   ├── schemas.py         # Contratos Pydantic de request/response
│   ├── deps.py            # Dependencias compartidas (usuario actual)
│   └── routers/
│       ├── fs.py          # RF1: ls/mkdir/rmdir/rm/stat (Jacobo)
│       └── transfer.py    # RF2: PUT/GET/HEAD de archivos (Paulina)
├── cli/                   # Cliente de línea de comandos (Mariana)
├── tests/
│   ├── conftest.py
│   ├── test_path_service.py
│   ├── test_fs.py         # Tests de RF1
│   └── test_transfer.py   # Tests de RF2
├── requirements.txt
├── CONTRATOS.md
└── README.md
```

## Cómo correrlo

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload
# -> http://localhost:8000/health
```

## Tests

```bash
pytest tests -q          # suite del servidor (RF1, RF2 y path_service)
```

La suite de la CLI vive aparte y necesita su propio instalable:

```bash
pip install -e ./cli && pytest cli/tests -q
```

## RF1 — Gestión del sistema de archivos (`app/routers/fs.py`)

Implementa `ls`, `mkdir`, `rmdir`, `rm` y `stat` sobre `/fs/*`, siguiendo
el contrato de la sección 1 de [`CONTRATOS.md`](./CONTRATOS.md).

- Toda la resolución de rutas se delega en `app/path_service.py`: este
  router no parte ni normaliza rutas por su cuenta.
- `mkdir` acepta `parents: true` (equivalente a `mkdir -p`): crea los
  directorios intermedios que falten y es idempotente.
- `rm` y `rmdir --recursive` borran también el **contenido en disco** de
  los archivos afectados, no solo sus metadatos. El orden es deliberado:
  primero se confirma la transacción y después se limpia el disco, de
  modo que un fallo al borrar deje blobs huérfanos (inofensivos) y nunca
  metadatos apuntando a contenido inexistente.
- `rmdir` sin `recursive` solo borra directorios vacíos
  (`409 directory_not_empty`), y la raíz `/` nunca se puede borrar.
- `stat` sirve tanto para archivos como para directorios; es lo que usa
  la CLI para validar un `cd` sin listar el directorio entero.

### Ejemplos (con el servidor corriendo en `localhost:8000`)

```bash
# Crear un árbol de directorios de una vez
curl -X POST -H "X-Username: jacobo" -H "Content-Type: application/json" \
  -d '{"path":"/docs/informes","parents":true}' http://localhost:8000/fs/mkdir

# Listar
curl -H "X-Username: jacobo" "http://localhost:8000/fs/ls?path=/docs"

# Metadatos de una entrada cualquiera
curl -H "X-Username: jacobo" "http://localhost:8000/fs/stat?path=/docs/informes"

# Borrar un archivo (metadatos + contenido)
curl -X DELETE -H "X-Username: jacobo" -H "Content-Type: application/json" \
  -d '{"path":"/docs/informes/prueba.txt"}' http://localhost:8000/fs/rm

# Borrar un subárbol completo
curl -X DELETE -H "X-Username: jacobo" -H "Content-Type: application/json" \
  -d '{"path":"/docs","recursive":true}' http://localhost:8000/fs/rmdir
```

## RF2 — Transferencia de archivos (`app/routers/transfer.py`)

Implementa `PUT`, `GET` y `HEAD` sobre `/files/{path}`, siguiendo el
contrato de la sección 2 de [`CONTRATOS.md`](./CONTRATOS.md).

- El body del `PUT` se recibe **en streaming** (bytes crudos, no
  multipart) y nunca se carga completo en memoria — importante para
  archivos grandes.
- Cada archivo se guarda en disco indexado por `file.id` (no por
  nombre), en `DFSHA_STORAGE_DIR` (por defecto `./data/blocks`).
- Se calcula un checksum SHA-256 al subir, guardado junto al blob, y se
  expone en el header `X-Checksum-Sha256` de `GET`/`HEAD` para que el
  cliente pueda verificar integridad.
- `PUT` no crea directorios intermedios: el directorio padre debe
  existir de antes (vía RF1).

Autenticación: igual que el resto del servicio, con el header
`X-Username` (ver `app/deps.py`).

### Ejemplos (con el servidor corriendo en `localhost:8000`)

```bash
# Subir un archivo
curl -X PUT -H "X-Username: pau" --data-binary "@archivo.txt" \
  http://localhost:8000/files/archivo.txt

# Ver metadata sin descargar contenido
curl -I -H "X-Username: pau" http://localhost:8000/files/archivo.txt

# Descargar
curl -H "X-Username: pau" http://localhost:8000/files/archivo.txt \
  -o descargado.txt
```

## Modelo de datos

- **users**: dueños de archivos y directorios (sin autenticación real en
  el Hito 1; ver `app/deps.py`).
- **directories**: árbol jerárquico autorreferenciado (`parent_id`). La
  raíz (`/`) es la fila con `parent_id = NULL`.
- **files**: hojas del árbol, siempre dentro de un `directory`. Guardan
  solo metadatos (nombre, tamaño, dueño, timestamps); el contenido lo
  maneja el módulo de transferencia.

Ver el diseño completo y las decisiones detrás en la conversación de
entrega, o directamente en los docstrings de `app/models.py` y
`app/path_service.py`.
