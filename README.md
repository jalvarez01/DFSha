# DFSha

Sistema de archivos distribuido por bloques, estilo HDFS.
**Hito 1**: versión monolítica cliente/servidor.

Stack: Python 3.11, FastAPI, SQLite (SQLAlchemy), Pydantic.

## Alcance de este repo en el Hito 1

Este documento cubre lo construido hasta ahora: **servidor core y
modelo de datos** (Juan) y **RF2 — transferencia de archivos**
(Paulina). El resto del sistema (endpoints de gestión de FS y la CLI)
se documenta y contratiza en [`CONTRATOS.md`](./CONTRATOS.md) para que
se implemente encima sin bloqueos.

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
│       └── transfer.py    # RF2: PUT/GET/HEAD de archivos (Paulina)
├── tests/
│   ├── conftest.py
│   ├── test_path_service.py
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
pytest -q
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
