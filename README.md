# DFSha

Sistema de archivos distribuido por bloques, estilo HDFS.
**Hito 1**: versión monolítica cliente/servidor.

Stack: Python 3.11, FastAPI, SQLite (SQLAlchemy), Pydantic.

## Alcance de este repo en el Hito 1

El Hito 1 está completo: **servidor core y modelo de datos** (Juan),
**RF1 — gestión del sistema de archivos** (Jacobo), **RF2 —
transferencia de archivos** (Paulina) y la **CLI** (Mariana). Las firmas
exactas de cada endpoint están en [`CONTRATOS.md`](./CONTRATOS.md).

La autenticación básica user/pass con JWT que pide el enunciado ya está:
`POST /auth/login` verifica credenciales y emite un token que todo
endpoint exige como `Authorization: Bearer <jwt>`.

Queda fuera del Hito 1: la arquitectura distribuida (Hito 2) y la alta
disponibilidad, replicación y seguridad avanzada (Hito 3). En particular,
el **control de acceso por usuario** es del Hito 3: hoy el token dice
quién hace cada operación y quién es el dueño de cada archivo, pero el
namespace sigue siendo global y no restringe nada.

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
│       ├── transfer.py    # RF2: PUT/GET/HEAD de archivos (Paulina, Hito 1)
│       ├── datanodes.py   # Hito 2: register/heartbeat de DataNodes (Juan)
│       └── control.py     # Hito 2: allocate/confirm/blocks (particionamiento, Juan)
├── datanode/              # Hito 2: servicio DataNode, independiente del ControlNode (Jacobo)
│   ├── app/
│   │   ├── config.py      # DFSHA_DATANODE_ID, DFSHA_CONTROLNODE_URL, etc.
│   │   ├── storage.py     # Storage local: archivo=directorio, bloque=archivo
│   │   ├── registration.py # register/heartbeat periódico contra el ControlNode
│   │   └── main.py        # PUT/GET/DELETE /blocks/{block_id}
│   ├── tests/
│   └── requirements.txt
├── cli/                   # Cliente de línea de comandos (Mariana)
├── tests/
│   ├── conftest.py
│   ├── test_path_service.py
│   ├── test_auth.py       # Tests de login/JWT
│   ├── test_fs.py         # Tests de RF1
│   ├── test_transfer.py   # Tests de RF2 (Hito 1, monolítico)
│   └── test_control.py    # Tests de Hito 2: placement, allocate/confirm/blocks
├── docker-compose.yml     # 1 ControlNode + 3 DataNodes
├── requirements.txt
├── CONTRATOS.md
└── README.md
```

## Hito 2 — Particionamiento y DFS distribuido por bloques

Decisiones de equipo (ver `CONTRATOS.md` para el detalle de cada endpoint):

- **Particionamiento por bloques** (no NFS-style, que no particiona): un
  archivo se corta en `ceil(size / block_size)` bloques y cada uno se
  asigna round-robin a un DataNode vivo (`app/placement.py`). El tamaño de
  bloque es **configurable** (`DFSHA_BLOCK_SIZE_BYTES`, no hay un único
  valor fijo de 64/128 MB); se puede pedir por archivo en el `allocate`.
- **Storage físico en el DataNode**: "archivo como directorio, bloque como
  archivo" — `datanode/app/storage.py` guarda cada bloque como
  `<storage_dir>/<file_id>/<block_id>`. Un DataNode nunca tiene el archivo
  completo, solo los bloques que le tocaron.
- **CRUD, no WORM**: además del `allocate` de archivo completo (que
  reemplaza todo el plan), `POST /files/{path}/blocks/{index}/allocate`
  permite reemplazar o anexar UN bloque puntual sin re-subir el archivo
  entero, y `confirm` ya soportaba confirmar un subconjunto de bloques.
- **Transparencia de localización**: el cliente le pide al ControlNode
  (rol NameNode) el plan (`allocate`/`blocks`, con host:puerto de cada
  bloque) y luego habla **directo** con cada DataNode — los bytes nunca
  pasan por el ControlNode.

Levantar el clúster local de prueba (1 ControlNode + 3 DataNodes):

```bash
docker compose up --build
```

## Cómo correrlo

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload
# -> http://localhost:8000/health
```

En cualquier despliegue que no sea desarrollo local hay que fijar el
secreto con el que se firman los JWT:

```bash
export DFSHA_JWT_SECRET="algo-largo-y-aleatorio"
```

## Tests

```bash
pytest tests -q          # suite del servidor (RF1, RF2 y path_service)
```

La suite de la CLI vive aparte y necesita su propio instalable:

```bash
pip install -e ./cli && pytest cli/tests -q
```

## Autenticación (`app/routers/auth.py`, `app/security.py`)

`POST /auth/login` recibe `{"username", "password"}` y devuelve un JWT
(HS256, 12 h por defecto) que hay que mandar en toda petición posterior
como `Authorization: Bearer <token>`.

- Las contraseñas se guardan hasheadas con PBKDF2-HMAC-SHA256 y salt
  aleatorio en `users.password_hash` (librería estándar, sin dependencias
  nativas que compilar).
- No hay endpoint de registro porque el enunciado solo pide autenticación
  básica: el primer login de un usuario sin contraseña fija la que envíe,
  y los siguientes la verifican (`401 invalid_credentials` si no cuadra).
- El header `X-Username` se sigue aceptando como fallback de desarrollo
  —no verifica nada—; si vienen los dos, gana el token.

```bash
# Obtener un token
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"mariana","password":"clave123"}' | jq -r .access_token)

# Usarlo
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/fs/ls?path=/"
```

Desde la CLI es transparente: `dfsha login <usuario>` pide la contraseña,
guarda el token en `~/.dfsha/session.json` (permisos 600) y el resto de
comandos lo usan solos.

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

### Ejemplos (con el servidor corriendo en `localhost:8000` y `$TOKEN` del login)

```bash
# Crear un árbol de directorios de una vez
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"path":"/docs/informes","parents":true}' http://localhost:8000/fs/mkdir

# Listar
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/fs/ls?path=/docs"

# Metadatos de una entrada cualquiera
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/fs/stat?path=/docs/informes"

# Borrar un archivo (metadatos + contenido)
curl -X DELETE -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"path":"/docs/informes/prueba.txt"}' http://localhost:8000/fs/rm

# Borrar un subárbol completo
curl -X DELETE -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
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

Autenticación: igual que el resto del servicio, con el JWT del login
(ver `app/deps.py`).

### Ejemplos (con el servidor corriendo en `localhost:8000` y `$TOKEN` del login)

```bash
# Subir un archivo
curl -X PUT -H "Authorization: Bearer $TOKEN" --data-binary "@archivo.txt" \
  http://localhost:8000/files/archivo.txt

# Ver metadata sin descargar contenido
curl -I -H "Authorization: Bearer $TOKEN" http://localhost:8000/files/archivo.txt

# Descargar
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/files/archivo.txt \
  -o descargado.txt
```

## Modelo de datos

- **users**: dueños de archivos y directorios, con `password_hash` para
  el login (ver `app/routers/auth.py`).
- **directories**: árbol jerárquico autorreferenciado (`parent_id`). La
  raíz (`/`) es la fila con `parent_id = NULL`.
- **files**: hojas del árbol, siempre dentro de un `directory`. Guardan
  solo metadatos (nombre, tamaño, dueño, timestamps); el contenido lo
  maneja el módulo de transferencia.

Ver el diseño completo y las decisiones detrás en la conversación de
entrega, o directamente en los docstrings de `app/models.py` y
`app/path_service.py`.
