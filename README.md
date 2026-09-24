# DFSha

Sistema de archivos distribuido por bloques, estilo HDFS.
**Hito 1**: versión monolítica cliente/servidor.
**Hito 2**: ControlNode + N DataNodes, con los archivos particionados en
bloques (ver [Hito 2](#hito-2--particionamiento-y-dfs-distribuido-por-bloques)).

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
│   ├── models.py          # Modelos SQLAlchemy: User, Directory, File, DataNode, Block, PendingDeletion
│   ├── placement.py       # Hito 2: liveness, partición y round-robin
│   ├── block_gc.py        # Hito 2: cola de borrado de bloques (vía heartbeat)
│   ├── security.py        # Hash de contraseñas y JWT
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
│   │   ├── registration.py # register/heartbeat + órdenes de borrado
│   │   ├── security.py    # validación del JWT del cliente
│   │   └── main.py        # PUT/GET/DELETE /blocks/{block_id}
│   ├── tests/
│   └── requirements.txt
├── cli/                   # Cliente de línea de comandos (Mariana)
│   └── dfsha_cli/distributed.py  # Hito 2: put/get por bloques en paralelo
├── docs/
│   └── ESPECIFICACION_HITO2.md   # Protocolos, APIs y diagramas de los 4 flujos
├── scripts/
│   └── verify_hito2.py    # Verificación end-to-end contra un clúster levantado
├── tests/
│   ├── conftest.py
│   ├── test_path_service.py
│   ├── test_auth.py       # Tests de login/JWT
│   ├── test_fs.py         # Tests de RF1
│   ├── test_transfer.py   # Tests de RF2 (Hito 1, monolítico)
│   ├── test_control.py    # Hito 2: placement, allocate/confirm/blocks, borrados
│   └── test_hito2_integracion.py  # Hito 2: convivencia con el Hito 1, migración
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
  pasan por el ControlNode. El usuario solo escribe `dfsha put archivo`.
- **Lecturas sin huecos**: mientras un reemplazo de bloque no se confirma,
  el plan de lectura sigue sirviendo la versión anterior.
- **Borrado distribuido vía heartbeat** (como HDFS): `rm`, `rmdir -r`, un
  re-`allocate` o un reemplazo confirmado encolan el borrado de los
  bloques viejos (`app/block_gc.py`); cada DataNode recibe la orden en la
  respuesta de su heartbeat y la acusa en el siguiente. El ControlNode
  nunca llama a los DataNodes, y un nodo caído recibe sus órdenes al
  volver.
- **Seguridad del DataNode**: exige el mismo JWT del login (secreto
  compartido `DFSHA_JWT_SECRET`) y valida `file_id`/`block_id`, así que no
  se puede escribir ni borrar fuera de su carpeta.
- **Integridad**: el cliente manda el SHA-256 de cada bloque
  (`X-Checksum-Sha256`); el DataNode rechaza lo que no coincide, guarda el
  checksum y verifica el bloque antes de servirlo, y el cliente lo verifica
  otra vez al descargar. Todo en streaming.

La especificación de protocolos, con los diagramas de los cuatro flujos y
el clúster en ejecución, está en
[`docs/ESPECIFICACION_HITO2.md`](docs/ESPECIFICACION_HITO2.md).

### Levantar el clúster y usarlo

```bash
docker compose up --build -d          # 1 ControlNode (:8000) + 3 DataNodes (:8001-8003)

pip install -e ./cli
dfsha login mariana                   # -u http://localhost:8000 por defecto
dfsha nodes                           # DataNodes y su estado
dfsha put video.bin                   # se parte en bloques de 8 MiB
dfsha get video.bin copia.bin         # baja en paralelo y verifica checksums
DFSHA_BLOCK_SIZE=65536 dfsha put README.md   # bloques pequeños para la demo

python scripts/verify_hito2.py        # verificación end-to-end automática
docker compose down                   # (-v para borrar también los datos)
```

Sin Docker, cada componente se puede levantar a mano:

```bash
uvicorn app.main:app --port 8000
cd datanode && DFSHA_DATANODE_ID=dn1 DFSHA_DATANODE_ADVERTISE_PORT=9001 \
  DFSHA_DATANODE_STORAGE_DIR=/tmp/dn1 uvicorn app.main:app --port 9001
```

`dfsha get` sigue pudiendo bajar archivos subidos con el `PUT` monolítico
del Hito 1. Al arrancar, el ControlNode migra solo la tabla `blocks` de
las BD de desarrollo creadas antes del reemplazo de bloques.

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
pytest tests -q                                   # ControlNode (RF1, RF2, Hito 2)
(cd datanode && pip install -r requirements.txt && pytest tests -q)   # DataNode
pip install -e ./cli && pytest cli/tests -q       # CLI (incluye put/get distribuido)
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
