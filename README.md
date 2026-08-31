# DFSha

Sistema de archivos distribuido por bloques, estilo HDFS.
**Hito 1**: versión monolítica cliente/servidor.

Stack: Python 3.11, FastAPI, SQLite (SQLAlchemy), Pydantic, Docker.

## Alcance de este repo en el Hito 1

Este documento cubre la parte construida hasta ahora: **servidor core y
modelo de datos**. El resto del sistema (endpoints de gestión de FS,
endpoints de transferencia y la CLI) se documenta y contratiza en
[`CONTRATOS.md`](./CONTRATOS.md) para que se implemente encima sin
bloqueos.

## Estructura del repo

```
DFSha/
├── app/
│   ├── main.py           # App FastAPI, /health, arranque
│   ├── config.py         # Settings (env vars)
│   ├── database.py       # engine, SessionLocal, get_db
│   ├── models.py         # Modelos SQLAlchemy: User, Directory, File
│   ├── path_service.py   # Resolución/validación de rutas (núcleo)
│   ├── schemas.py        # Contratos Pydantic de request/response
│   └── deps.py           # Dependencias compartidas (usuario actual)
├── tests/
│   ├── conftest.py
│   └── test_path_service.py
├── requirements.txt
├── Dockerfile
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

Con Docker:

```bash
docker build -t dfsha .
docker run -p 8000:8000 -v dfsha_data:/data dfsha
```

## Tests

```bash
pytest -q
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
