# DFSha

Sistema de archivos distribuido por bloques, estilo HDFS.
**Hito 1**: versión monolítica cliente/servidor.
**Hito 2**: ControlNode + N DataNodes, con los archivos particionados en
bloques (ver [Hito 2](#hito-2--particionamiento-y-dfs-distribuido-por-bloques)).
**Hito 3**: alta disponibilidad, replicación, consistencia y seguridad (ver
[Hito 3](#hito-3--alta-disponibilidad-replicación-consistencia-y-seguridad)).

Stack: Python 3.11, FastAPI, SQLite (SQLAlchemy), Pydantic.

## Alcance de este repo en el Hito 1

El Hito 1 está completo: **servidor core y modelo de datos** (Juan),
**RF1 — gestión del sistema de archivos** (Jacobo), **RF2 —
transferencia de archivos** (Paulina) y la **CLI** (Mariana). Las firmas
exactas de cada endpoint están en [`CONTRATOS.md`](./CONTRATOS.md).

La autenticación básica user/pass con JWT que pide el enunciado ya está:
`POST /auth/login` verifica credenciales y emite un token que todo
endpoint exige como `Authorization: Bearer <jwt>`.

Quedaban fuera del Hito 1 la arquitectura distribuida (Hito 2) y la alta
disponibilidad, replicación y seguridad avanzada (Hito 3), incluido el
control de acceso por usuario. Ambos están implementados más abajo.

## Estructura del repo

```
DFSha/
├── app/                   # ControlNode
│   ├── main.py            # App FastAPI, /health, migraciones, bucles de HA y re-replicación
│   ├── config.py          # Settings (env vars)
│   ├── database.py        # engine, SessionLocal, get_db
│   ├── models.py          # Modelos SQLAlchemy (namespace, bloques, réplicas, locks, grupos, ACL, HA)
│   ├── path_service.py    # Resolución/validación de rutas (núcleo)
│   ├── schemas.py         # Contratos Pydantic de request/response
│   ├── deps.py            # Usuario actual (API key, JWT)
│   ├── security.py        # Contraseñas, JWT, TOTP (2FA), API keys
│   ├── acl.py             # Hito 3: permisos estilo Unix, grupos y ACL
│   ├── locks.py           # Hito 3: locks de archivo y leases de escritura
│   ├── cluster_auth.py    # Hito 3: tokens entre nodos y tokens de bloque
│   ├── placement.py       # Liveness, partición y cadenas de réplica
│   ├── replication.py     # Hito 3: re-replicación, sobrantes, conciliación
│   ├── block_gc.py        # Cola de borrado de bloques (vía heartbeat)
│   ├── ha/                # Hito 3: ControlNode líder/respaldo (log de cambios, failover)
│   └── routers/
│       ├── auth.py        # login, 2FA, API keys
│       ├── fs.py          # RF1: ls/mkdir/rmdir/rm/stat + chmod/chgrp/acl
│       ├── groups.py      # Hito 3: grupos
│       ├── transfer.py    # RF2 monolítico del Hito 1
│       ├── datanodes.py   # register/heartbeat/informes de DataNodes
│       ├── control.py     # allocate/confirm/blocks + locks
│       └── cluster.py     # Hito 3: /cluster/status
├── datanode/              # Servicio DataNode
│   ├── app/
│   │   ├── config.py      # Variables DFSHA_DATANODE_*, ControlNodes, TLS, clave
│   │   ├── storage.py     # archivo=directorio, bloque=archivo; AES-GCM; inmutables
│   │   ├── registration.py # heartbeat a todos los ControlNodes, órdenes, inventario
│   │   ├── cluster.py     # líder conocido, época, informes pendientes
│   │   ├── security.py    # tokens de bloque y de nodo
│   │   └── main.py        # PUT/GET/DELETE /blocks/{block_id}, cadena de réplica
│   └── tests/
├── cli/                   # Cliente de línea de comandos
│   └── dfsha_cli/distributed.py  # put/get por bloques con réplicas y failover
├── docs/
│   ├── ESPECIFICACION_HITO2.md   # Protocolos y diagramas del Hito 2
│   └── ESPECIFICACION_HITO3.md   # HA, replicación, consistencia, seguridad; los 5 pares de comunicación
├── scripts/
│   ├── setup_cluster.py   # Hito 3: CA, certificados por nodo y secretos (.env)
│   ├── verify_hito2.py    # Verificación del Hito 2
│   └── verify_hito3.py    # Verificación del Hito 3 con caídas reales (docker compose)
├── tests/                 # Tests del ControlNode
├── docker-compose.yml     # 2 ControlNodes + 4 DataNodes, TLS
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
- **Seguridad del DataNode**: valida `file_id`/`block_id`, así que no se
  puede escribir ni borrar fuera de su carpeta. En el Hito 2 exigía el JWT
  del login; desde el Hito 3 exige un token de bloque firmado por el
  ControlNode.
- **Integridad**: el cliente manda el SHA-256 de cada bloque
  (`X-Checksum-Sha256`); el DataNode rechaza lo que no coincide, guarda el
  checksum y verifica el bloque antes de servirlo, y el cliente lo verifica
  otra vez al descargar. Todo en streaming.

La especificación de protocolos, con los diagramas de los cuatro flujos y
el clúster en ejecución, está en
[`docs/ESPECIFICACION_HITO2.md`](docs/ESPECIFICACION_HITO2.md).

## Hito 3 — Alta disponibilidad, replicación, consistencia y seguridad

Todo el detalle (diagramas de los cinco pares de comunicación, algoritmos,
modelo de consistencia y límites) está en
[`docs/ESPECIFICACION_HITO3.md`](docs/ESPECIFICACION_HITO3.md). Las firmas,
en la sección "Hito 3" de [`CONTRATOS.md`](./CONTRATOS.md).

**Alta disponibilidad y replicación (RNF2)**
- Cada bloque se guarda en **3 DataNodes**. El cliente lo sube una sola vez
  al primero de su cadena y cada DataNode lo reenvía al siguiente
  (DataNode → DataNode).
- Si un DataNode cae, el archivo se sigue leyendo desde otras réplicas, y
  el ControlNode ordena copiar sus bloques a otro nodo hasta recuperar el
  factor. Cuando el nodo vuelve, se quitan las réplicas sobrantes.
- **Dos ControlNodes**: el líder replica cada cambio de metadatos al
  respaldo (log de cambios capturado del ORM, replicación semi-síncrona). Si
  el líder cae, el respaldo toma el control en unos 15 s, y la CLI cambia
  sola de uno a otro. Para evitar dos líderes, el respaldo solo se promueve
  si los DataNodes tampoco ven al líder; las épocas descartan órdenes de un
  líder viejo.

**Consistencia (RNF3)**
- Un bloque solo se confirma si los DataNodes informan haberlo guardado con
  ese checksum.
- Los bloques son inmutables.
- Una lectura nunca ve huecos ni pierde un bloque a mitad (hay un periodo de
  gracia antes de borrar).
- Locks y leases evitan que dos escritores se pisen (RF3 `lock()`).

**Seguridad (RNF6)**
- **TLS** en todas las conexiones, con una CA propia del clúster.
- **Cifrado en reposo**: AES-256-GCM en cada DataNode.
- **Control de acceso** estilo Unix: dueño, grupo, `rwx`, sticky bit,
  **grupos** y **ACL**. Lo nuevo es privado por defecto.
- **Autenticación**: user/password, **2FA (TOTP)**, **API keys** y bloqueo
  por fuerza bruta.
- **Entre nodos**: tokens firmados con un secreto de clúster. El DataNode
  solo acepta **tokens de bloque** que firma el ControlNode tras comprobar
  los permisos.

### Levantar el clúster del Hito 3

```bash
pip install cryptography
python scripts/setup_cluster.py       # CA + certificados por nodo (certs/) y secretos (.env); no van a git
docker compose up --build -d          # cn1 (:8000, líder), cn2 (:8010, respaldo), 4 DataNodes (:8001-8004)

pip install -e ./cli
dfsha login ana -u https://localhost:8000,https://localhost:8010 --ca-cert certs/ca.crt
dfsha put video.bin && dfsha status   # 3 réplicas por bloque
dfsha chmod 640 /video.bin            # permisos; también chgrp, acl, group
dfsha 2fa setup                       # 2FA; también apikey create
dfsha lock /video.bin                 # lock exclusivo (RF3)

python scripts/verify_hito3.py        # ~1-3 min: replicación, seguridad, locks, caída de un DataNode y del líder
docker compose stop controlnode-1     # o probarlo a mano: la CLI sigue funcionando con cn2
```

**Al migrar bases existentes**: el ControlNode añade solo las columnas y
tablas nuevas al arrancar. Lo que ya existía queda con permisos 0644/0755
(legible por todos, escribible solo por su dueño), la raíz pasa a 1777 y
cada bloque viejo queda con su única réplica.

## Cómo correrlo

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload
# -> http://localhost:8000/health
```

En cualquier despliegue que no sea desarrollo local hay que fijar los
secretos con los que se firman los JWT y los tokens entre nodos
(`scripts/setup_cluster.py` los genera en `.env`):

```bash
export DFSHA_JWT_SECRET="algo-largo-y-aleatorio"
export DFSHA_CLUSTER_SECRET="otro-distinto"
```

## Tests

```bash
pytest tests -q                                   # ControlNode: RF1, RF2, Hitos 2 y 3 (ACL, 2FA, locks, réplicas, HA)
(cd datanode && pip install -r requirements.txt && pytest tests -q)   # DataNode: tokens, cifrado, cadena, heartbeat
pip install -e ./cli && pytest cli/tests -q       # CLI: put/get con réplicas y failover
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
- El header `X-Username` (no verifica nada) solo se acepta con
  `DFSHA_ALLOW_INSECURE_DEV_AUTH=true`, para tests y desarrollo: con control
  de acceso permitiría suplantar a cualquiera.
- Hito 3: 2FA (TOTP) opcional por usuario, API keys para scripts y bloqueo
  temporal tras 5 logins fallidos. Ver la sección del Hito 3.

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
