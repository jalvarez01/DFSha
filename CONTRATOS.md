# Contratos de API — DFSha (Hitos 1, 2 y 3)

Este documento fija la firma exacta de los endpoints que **no** implemento
yo, para que cada compañero pueda empezar a codear en paralelo sin
esperar a que el resto avance. Los modelos Pydantic mencionados ya están
definidos en `app/schemas.py`; impórtenlos directamente, no los
reescriban.

Todos los endpoints deben:
- Recibir `db: Session = Depends(get_db)` (de `app.database`).
- Recibir `user: User = Depends(get_current_user)` (de `app.deps`) para
  saber quién es el dueño de la operación (header `Authorization: Bearer
  <jwt>`, o el header heredado `X-Username`; ver sección 0).
- Resolver rutas usando **únicamente** las funciones de
  `app/path_service.py` (no reimplementar split/normalize a mano).
- Capturar las excepciones de `path_service` y traducirlas a HTTP así:

| Excepción                        | Código HTTP | `error` |
|-----------------------------------|-------------|---------|
| `InvalidPathError`                 | 400 | `invalid_path` |
| `NotFoundError`                    | 404 | `not_found` |
| `NotADirectoryError`               | 409 | `not_a_directory` |
| `NotAFileError`                    | 409 | `not_a_file` |
| `AlreadyExistsError`               | 409 | `already_exists` |
| `RootOperationError`               | 400 | `root_operation` |
| `DirectoryNotEmptyError` (propia de `fs.py`) | 409 | `directory_not_empty` |

Fuera de esa tabla, la app devuelve además `401 unauthenticated` (no vino
ningún header de autenticación), `401 invalid_token` (JWT inválido o
expirado), `401 invalid_credentials` (login fallido) y `422 validation_error` (cuerpo de
request malformado; un manejador en `app/main.py` lo reescribe al formato
`ErrorResponse` en vez del `detail` en forma de lista que trae FastAPI).

Desde el Hito 3 (ver la sección "Hito 3" al final):

| HTTP | `error` | Cuándo |
|---|---|---|
| 401 | `otp_required`, `invalid_otp` | login de un usuario con 2FA |
| 401 | `invalid_api_key` | API key inválida, revocada o expirada |
| 401 | `invalid_node_token` | `/datanodes/*` o `/ha/*` sin token de nodo válido |
| 403 | `permission_denied` | el usuario no tiene permiso (app/acl.py) |
| 403 | `session_required` | operación sensible hecha con API key |
| 403 | `node_mismatch` | un DataNode habla en nombre de otro |
| 409 | `file_locked`, `invalid_lease` | locks y leases de escritura |
| 409 | `replicas_not_ready` | `confirm` sin informes de los DataNodes que lo respalden |
| 410 | `snapshot_required` | `/ha/log`: el respaldo debe pedir un snapshot |
| 429 | `too_many_attempts` | bloqueo por logins fallidos |
| 503 | `not_leader` | petición a un ControlNode respaldo (header `X-DFSha-Leader`) |

El cuerpo de cualquier error debe tener la forma de `ErrorResponse`
(`{"error": "...", "message": "..."}"`), típicamente vía:

```python
raise HTTPException(status_code=404, detail=ErrorResponse(
    error="not_found", message=str(exc)
).model_dump())
```

Todos los endpoints con ruta como parámetro aceptan opcionalmente un
`cwd_id` (id de un `Directory`) para resolver rutas relativas. Si no se
manda y la ruta es relativa, `path_service` lanza `InvalidPathError`. Para
el Hito 1 monolítico, lo más simple es que **la CLI siempre mande rutas
absolutas** y nadie necesite preocuparse por `cwd_id`; lo dejamos definido
por si se quiere soportar `cd` más adelante.

---

## 0. Autenticación — `POST /auth/login`

**Estado: implementado** en `app/routers/auth.py`, registrado en `main.py`
con `prefix="/auth"`. Es el único endpoint público (no exige autenticación).

Request: `LoginRequest` — `{"username": str, "password": str}`.

Response `200`: `TokenResponse`

```json
{
  "access_token": "<jwt>",
  "token_type": "bearer",
  "expires_in": 43200,
  "username": "mariana"
}
```

- El token es un JWT HS256 con `sub` (username), `uid` (id del usuario),
  `iat` y `exp`. Vive `DFSHA_JWT_EXPIRE_MINUTES` (12 h por defecto) y se
  firma con `DFSHA_JWT_SECRET` — variable obligatoria fuera de desarrollo.
- Las contraseñas se guardan hasheadas con PBKDF2-HMAC-SHA256 (con salt) en
  `users.password_hash`. Ver `app/security.py`.
- No hay endpoint de registro: el primer login de un username sin
  contraseña fija la que se envíe, y los siguientes la verifican. Con
  credenciales incorrectas responde `401 invalid_credentials`, con el mismo
  mensaje siempre para no revelar qué usernames existen.

El cliente manda el token en cada petición posterior:

```
Authorization: Bearer <access_token>
```

`X-Username` se sigue aceptando como fallback de desarrollo (no verifica
nada y autocrea el usuario); si vienen los dos headers, gana el token.
El control de acceso por usuario —impedir que un usuario toque los
archivos de otro— es del Hito 3: hoy el namespace es global.

---

## 1. RF1 — gestión del FS: `ls` / `mkdir` / `rmdir` / `rm` / `stat`

**Estado: implementado** en `app/routers/fs.py`, registrado en `main.py`
con `prefix="/fs"`.

Todos aceptan además el query param opcional `cwd_id: int | None`. Si se
manda un `cwd_id` inexistente, la respuesta es `404`.

### `GET /fs/ls`

Lista el contenido de un directorio.

- Query params: `path: str` (default `"/"`), `cwd_id`.
- Respuesta `200`: `ListDirectoryResponse`. Los subdirectorios van antes
  que los archivos y cada grupo llega ordenado por nombre. Las entradas de
  tipo `directory` siempre traen `size_bytes: 0`.
- Errores: `404` si el directorio no existe, `409 not_a_directory` si
  `path` apunta a un archivo.

### `POST /fs/mkdir`

Crea un directorio.

- Body: `MkdirRequest` (`{"path": "...", "parents": false}`).
- Con `parents: false` (default) el padre debe existir ya.
- Con `parents: true` se comporta como `mkdir -p`: crea los directorios
  intermedios que falten y es idempotente si el destino ya existe.
- Respuesta `201`: `DirectoryResponse`. Excepción: con `parents: true` y
  un destino que ya existía, responde `200` (no creó nada).
- Errores: `404` (el padre no existe), `409 already_exists` (ya hay un
  archivo o directorio con ese nombre), `409 not_a_directory` (un
  componente intermedio es un archivo), `400` (ruta inválida o `/`).

### `DELETE /fs/rmdir`

Borra un directorio.

- Body: `RmdirRequest` (`{"path": "...", "recursive": false}`).
- Con `recursive: false`, un directorio con contenido responde
  `409 directory_not_empty` sin borrar nada. Con `recursive: true` se
  borra el subárbol completo, **incluido el contenido en disco** de los
  archivos descendientes.
- Respuesta `200`: `DeleteResponse`.
- Errores: `400 root_operation` (no se puede borrar `/`), `404`,
  `409 not_a_directory` (`path` es un archivo → usar `rm`).

### `DELETE /fs/rm`

Borra un archivo, tanto sus metadatos como su contenido en disco.

- Body: `RmRequest` (`{"path": "..."}`).
- Respuesta `200`: `DeleteResponse`.
- Errores: `404` (no existe), `409 not_a_file` (`path` es un directorio →
  usar `rmdir`).

### `GET /fs/stat`

Metadatos de una entrada cualquiera, sea archivo o directorio. Permite a
la CLI validar un `cd` sin listar el directorio entero.

- Query params: `path: str` (default `"/"`), `cwd_id`.
- Respuesta `200`: `StatResponse`. Para directorios, `size_bytes` es `0` y
  `updated_at` es `null`; la raíz se reporta con `name: "/"`.
- Errores: `404` (no existe), `409 not_a_directory` (un componente
  intermedio es un archivo), `400` (ruta inválida).

---

## 2. RF2 — transferencia: `put` / `get`

**Estado: implementado** en `app/routers/transfer.py`, registrado en
`main.py` con `prefix="/files"`.

Este servidor (mi parte) solo guarda **metadatos** de archivo en la tabla
`files` (nombre, tamaño, dueño, ubicación). El almacenamiento del
contenido/bloques en sí es responsabilidad de este compañero — puede
guardarlo donde le convenga (disco local, otra tabla, bloques separados),
mientras mantenga sincronizado `File.size_bytes` con lo realmente
almacenado.

### `PUT /files/{path:path}`

Sube (crea o sobreescribe) un archivo. `path` va en la URL, no en el body
(usar un path converter de FastAPI: `{path:path}`), o alternativamente
recibir la ruta como query param si prefieren evitar problemas de
encoding con `/`.

- Body: el contenido del archivo (`UploadFile` o streaming, a su criterio).
- Flujo con `path_service`:
  1. `resolve_parent_and_name(db, path)` → obtiene el directorio padre
     (debe existir; si no, `404`).
  2. `find_child_file(db, parent.id, leaf_name)`: si existe, es un
     update (`created=False`); si no, `created=True`.
  3. Si `find_child_directory(db, parent.id, leaf_name)` existe, lanzar
     `NotADirectoryError`-equivalente → `409` (ya hay un directorio con
     ese nombre).
  4. Guardar el contenido donde corresponda, crear/actualizar la fila
     `File` con `size_bytes` real.
- Respuesta `200` o `201`: `PutFileResponse`.

### `GET /files/{path:path}`

Descarga un archivo.

- Usa `path_service.resolve_file(db, path)` para obtener metadatos y
  ubicar el contenido almacenado.
- Respuesta: `StreamingResponse` con el contenido y los headers
  `Content-Length` y `X-Checksum-Sha256`.
- Errores: `404` (no existe), `409` (`path` es un directorio).

### `HEAD /files/{path:path}`

Igual que el `GET` pero sin cuerpo: devuelve solo los headers
`Content-Length` y `X-Checksum-Sha256`, para conocer tamaño y checksum sin
descargar. Para los metadatos completos en JSON, usar `GET /fs/stat`.

---

## 3. Compañero de CLI

No expone contrato de servidor propio: consume los endpoints anteriores
por HTTP. Contrato que sí debe respetar:

- Mandar siempre el header `X-Username` (username elegido por el
  usuario de la CLI; se autocrea en el servidor si no existe, ver
  `app/deps.py::get_current_user`).
- Interpretar los errores según la tabla de códigos HTTP de arriba,
  leyendo `detail.error` / `detail.message` del `ErrorResponse`.
- Base URL configurable (variable de entorno o flag), por defecto
  `http://localhost:8000`.

---

---

# Hito 2 — ControlNode (DFS distribuido por bloques)

Estos endpoints los implemento yo (Juan) en el ControlNode. Fijan el
contrato para que los demás codeen en paralelo:

- **Jacobo (DataNode)** consume `register` / `heartbeat`.
- **Paulina (RF2 distribuido)** consume `allocate` → sube bloques a los
  DataNodes → `confirm`; y `blocks` para leer.
- **Mariana (cliente + spec)** documenta estos 4 flujos y propaga el JWT.

Modelos Pydantic nuevos en `app/schemas.py` (importarlos, no reescribirlos):
`DataNodeRegisterRequest`, `HeartbeatRequest`, `DataNodeInfo`,
`HeartbeatResponse`, `DataNodeLocation`, `AllocateRequest`,
`AllocatedBlock`, `BlockAllocateRequest`, `AllocateResponse`, `ConfirmBlock`,
`ConfirmRequest`, `ConfirmResponse`, `BlockPlacement`, `BlocksResponse`.

**Decisiones de equipo (cerradas):**
- **Tamaño de bloque: configurable**, sin un default único fijo de
  64/128 MB — se controla con `DFSHA_BLOCK_SIZE_BYTES` (o por archivo, en
  el body de `allocate`).
- **CRUD, no WORM**: se puede actualizar o anexar un bloque puntual sin
  re-subir el archivo completo (ver `blocks/{index}/allocate` más abajo).
- **Storage físico del DataNode**: "archivo como directorio, bloque como
  archivo" (`<storage_dir>/<file_id>/<block_id>`) — así un DataNode nunca
  tiene el archivo completo, solo sus bloques. Por eso `AllocatedBlock` y
  `BlockPlacement` incluyen `file_id`: es lo que el cliente manda como
  `?file_id=` al hacer `PUT {datanode}/blocks/{block_id}`.

- **Borrado distribuido vía heartbeat** (como el NameNode de HDFS): el
  ControlNode nunca llama a los DataNodes. Cuando un bloque sale del
  namespace se encola en `pending_deletions` y viaja en la respuesta del
  heartbeat (ver sección 4).
- **JWT también en el DataNode**: el cliente manda el mismo token del
  login; el DataNode lo valida con el secreto compartido
  (`DFSHA_JWT_SECRET`).

Tablas nuevas en `app/models.py`: `datanodes` (host, puerto, estado,
último heartbeat), `blocks` (block_id, file_id, índice, tamaño, checksum,
datanode_id, committed; único por `(file_id, block_index, committed)`) y
`pending_deletions` (datanode_id, block_id, file_id). Al arrancar,
`app/main.py` migra la tabla `blocks` de BD creadas con la unicidad vieja
`(file_id, block_index)`.

La especificación completa de protocolos, con los diagramas de los cuatro
flujos, está en [`docs/ESPECIFICACION_HITO2.md`](docs/ESPECIFICACION_HITO2.md).

## 4. DataNode ↔ ControlNode — `app/routers/datanodes.py` (prefix `/datanodes`)

**Sin autenticación de usuario** (tráfico interno nodo↔control). Desde el
Hito 3 exigen un **token de nodo** y hay campos y endpoints nuevos: ver
§H3.4.

### `POST /datanodes/register`

Registra un DataNode al arrancar; idempotente por `node_id` (si ya existe,
actualiza host/puerto y lo reactiva). Cuenta como un heartbeat.

- Body: `DataNodeRegisterRequest` (`{"node_id": str, "host": str, "port": int}`).
- Respuesta `200`: `DataNodeInfo`.

### `POST /datanodes/heartbeat`

Refresca el heartbeat de un DataNode ya registrado y le entrega las órdenes
de borrado pendientes (RF1 distribuido).

- Body: `HeartbeatRequest` —
  `{"node_id": str, "deleted_blocks": [str]}`. `deleted_blocks` (opcional,
  `[]` por defecto) acusa los bloques que el nodo ya borró.
- Respuesta `200`: `HeartbeatResponse` —
  `{"node_id", "status", "last_heartbeat", "delete_blocks": [str]}`.
  `delete_blocks` son los `block_id` que el nodo debe borrar de su disco
  (máx. 1000 por latido).
- Entrega **al-menos-una-vez**: una orden se repite en cada heartbeat
  hasta que el nodo la acusa en `deleted_blocks`. Borrar es idempotente,
  así que el nodo acusa también los bloques que ya no tenía.
- Se encola una orden cuando un bloque sale del namespace: `rm`,
  `rmdir --recursive`, re-`allocate` de un archivo, confirmación de un
  reemplazo de bloque, o `PUT` del Hito 1 sobre un archivo distribuido.
- Errores: `404 unknown_datanode` (el node_id no está registrado → debe
  llamar antes a `/register`).

### `GET /datanodes`

Lista los DataNodes conocidos con su liveness **calculada** al momento
(`status`: `alive`/`dead` según heartbeat_ttl, no solo el último valor).

- Respuesta `200`: `list[DataNodeInfo]`.

**Liveness:** un DataNode está "vivo" si su último heartbeat cae dentro de
`DFSHA_HEARTBEAT_TTL_SECONDS` (30 s por defecto). Los DataNodes deben latir
a un intervalo holgadamente menor (p. ej. 10 s).

## 5. Cliente ↔ ControlNode — `app/routers/control.py` (prefix `/files`)

> **Hito 3:** allocate devuelve una cadena de réplica y un token por bloque,
> confirm exige informes de los DataNodes, blocks devuelve todas las
> réplicas, y todo comprueba permisos y locks. Ver §H3.3.

**Requieren autenticación** (`get_current_user`), igual que el resto de la
API de archivos. Traducen las excepciones de `path_service` con la misma
tabla de códigos de arriba.

> **Enrutado:** estas rutas usan `/files/{path:path}/<acción>` y conviven
> con el catch-all `/files/{path:path}` de transferencia. El router de
> control se registra **antes** en `main.py`. Consecuencia: `allocate`,
> `confirm` y `blocks` quedan reservados como último segmento de una ruta
> bajo `/files`.

### `POST /files/{path}/allocate` — plan de escritura (fase 1)

El cliente informa el tamaño total; el ControlNode parte el archivo en
bloques (`ceil(size / block_size)`; el último lleva el resto), los asigna
**round-robin sobre DataNodes vivos**, persiste las filas `blocks` en
`committed=False` y devuelve dónde subir cada uno. No recibe contenido.

- Body: `AllocateRequest` (`{"size_bytes": int, "block_size": int | null}`).
  Si `block_size` es null se usa `DFSHA_BLOCK_SIZE_BYTES` (8 MiB).
- Respuesta `200`: `AllocateResponse` — `blocks: [AllocatedBlock]`, cada
  uno con `index`, `block_id`, `size_bytes` y `datanode` (host/puerto).
- Un archivo vacío (`size_bytes: 0`) devuelve `blocks: []`.
- Re-allocate sobre un archivo existente descarta el plan anterior (mismo
  `file_id`, nuevos `block_id`) y ordena borrar sus bloques en los
  DataNodes. Si el archivo venía del `PUT` del Hito 1, se borra su blob
  local.
- Errores: `404 not_found` (el padre no existe), `409 not_a_directory`
  (ya hay un directorio con ese nombre), `503 no_datanodes` (no hay
  DataNodes vivos), `400 invalid_path`.

El cliente sube cada bloque a su DataNode: `PUT {datanode}/blocks/{block_id}?file_id={file_id}`.

### `POST /files/{path}/blocks/{index}/allocate` — escritura CRUD de UN bloque

**Estado: implementado.** Complementa `allocate` (que reemplaza el
archivo completo) con una escritura puntual, para la decisión de equipo de
soportar CRUD: reemplaza un bloque existente o anexa uno nuevo al final,
sin tocar ni re-subir los demás bloques.

- Body: `BlockAllocateRequest` (`{"size_bytes": int}`).
- `index < número de bloques`: reemplaza ese bloque. Crea una fila
  **pendiente** con nuevo `block_id` (posiblemente en otro DataNode); la
  versión confirmada sigue en el plan de lectura hasta que se confirme la
  nueva, así que un lector concurrente nunca ve un hueco. Al confirmar, la
  vieja sale del namespace y se ordena su borrado a su DataNode. Pedir otro
  reemplazo del mismo índice antes de confirmar descarta el anterior.
- `index == número de bloques`: anexa un bloque al final.
- Respuesta `200`: `AllocatedBlock` (un solo bloque, no una lista).
- Errores: `404 not_found` (el archivo no existe — debe existir ya, aunque
  sea vacío, vía un `allocate` previo), `409 not_a_file`,
  `400 invalid_block_index` (`index` deja un hueco, es decir
  `index > len(blocks)`), `503 no_datanodes`.
- El cliente sube el bloque igual que en el flujo normal
  (`PUT {datanode}/blocks/{block_id}?file_id={file_id}`) y cierra con el
  `/confirm` de siempre, mandando solo ese bloque — `confirm` ya soporta
  confirmar un subconjunto sin afectar a los que no cambiaron.
- **Nota de enrutado:** esta ruta se registra ANTES que `allocate` en
  `app/routers/control.py` porque `{path:path}/allocate` es greedy y
  también calzaría con `.../blocks/{index}/allocate`.

### `POST /files/{path}/confirm` — cierre de la escritura (fase 2)

Tras subir los bloques, el cliente reporta checksums y tamaños reales. El
ControlNode marca esos bloques `committed=True` y recalcula el tamaño del
archivo como la suma de los bloques confirmados.

- Body: `ConfirmRequest` (`{"blocks": [{"block_id", "checksum", "size_bytes"}]}`).
- Respuesta `200`: `ConfirmResponse` (`complete=true` si no queda ningún
  bloque planificado sin confirmar).
- Confirmar un bloque que reemplaza a otro del mismo índice borra el viejo
  del namespace y encola su borrado físico.
- Errores: `404 not_found` (el archivo no existe), `409 not_a_file`,
  `400 unknown_block` (un `block_id` no pertenece a ese archivo).

### `GET /files/{path}/blocks` — plan de lectura

Devuelve, en orden por `index`, de qué DataNode bajar cada bloque
**confirmado** y con qué checksum verificarlo. El cliente descarga
(`GET {datanode}/blocks/{block_id}`), verifica y reensambla.

- Respuesta `200`: `BlocksResponse` — `blocks: [BlockPlacement]`.
- Un archivo sin bloques confirmados devuelve `blocks: []`.
- Errores: `404 not_found`, `409 not_a_file`.

## 6. Servicio DataNode — `datanode/`

> **Hito 3:** el DataNode ya no valida el JWT del usuario sino un token de
> bloque firmado por el ControlNode; además reenvía por la cadena de réplica,
> cifra en reposo y no permite sobrescribir un bloque. Ver §H3.5. Lo que
> sigue describe el Hito 2.

Servicio FastAPI independiente del ControlNode. En el Hito 2, todos los
endpoints salvo `/health` exigían `Authorization: Bearer <jwt>` (el token de
`POST /auth/login` del ControlNode, validado con `DFSHA_JWT_SECRET`):
`401 unauthenticated` si falta, `401 invalid_token` si la firma no cuadra o
expiró.

`block_id` y `file_id` solo admiten `[A-Za-z0-9_-]{1,128}`; cualquier otro
valor (p. ej. `..`) es `400 invalid_id` y no toca el disco.

- `PUT /blocks/{block_id}?file_id=...` — sube/reemplaza un bloque en
  streaming. Guarda el contenido en `<storage_dir>/<file_id>/<block_id>`
  y su SHA-256 en `<block_id>.sha256`. Header opcional
  `X-Checksum-Sha256`: si no coincide con lo recibido, `400
  checksum_mismatch` y el bloque no se guarda (la versión anterior queda
  intacta). Respuesta `200`:
  `{"block_id", "file_id", "size_bytes", "checksum"}`.
- `GET /blocks/{block_id}` — descarga un bloque en streaming, con headers
  `Content-Length` y `X-Checksum-Sha256`. Antes de servirlo verifica el
  contenido contra el checksum guardado: `500 corrupt_block` si no
  coincide, `404 not_found` si no existe.
- `DELETE /blocks/{block_id}` — borra un bloque suelto (`404` si no existe).
- `DELETE /files/{file_id}/blocks` — borra todos los bloques de un archivo
  en ese nodo (uso manual/administración; el flujo normal de borrado va por
  el heartbeat).
- `GET /health`.

Al arrancar se registra contra el ControlNode y luego late cada
`DFSHA_HEARTBEAT_INTERVAL` segundos (`datanode/app/registration.py`),
reintentando si falla, así que puede arrancar antes que el ControlNode. En
cada respuesta ejecuta las órdenes `delete_blocks` y las acusa en el
heartbeat siguiente. Si el ControlNode responde `404 unknown_datanode`, se
vuelve a registrar.

Variables de entorno (`datanode/app/config.py`, prefijo `DFSHA_`):
`DATANODE_ID`, `CONTROLNODE_URL`, `DATANODE_ADVERTISE_HOST`,
`DATANODE_ADVERTISE_PORT` (también el puerto en el que escucha uvicorn,
ver `datanode/Dockerfile`), `HEARTBEAT_INTERVAL`, `DATANODE_STORAGE_DIR`,
`JWT_SECRET`.

## 7. Cliente — `cli/`

`dfsha put` y `dfsha get` usan el flujo distribuido
(`cli/dfsha_cli/distributed.py`): `allocate`, subida de cada bloque en
paralelo a su DataNode con `X-Checksum-Sha256`, y `confirm`; para leer,
`blocks` y descarga paralela verificando el checksum de cada bloque. Manda
el JWT a ControlNode y DataNodes. Un archivo subido con el `PUT` del
Hito 1 (sin bloques) se descarga por el `GET` monolítico.

`GET /files/{path}` del Hito 1 sobre un archivo distribuido responde
`409 distributed_file` (el ControlNode no tiene su contenido).

## Infraestructura (Hito 2)

`docker-compose.yml` levantaba 1 ControlNode y 3 DataNodes. En el Hito 3 son
2 ControlNodes y 4 DataNodes con TLS: ver §H3.8.

---

# Hito 3 — Alta disponibilidad, replicación, consistencia y seguridad

Diseño, diagramas y razones en
[`docs/ESPECIFICACION_HITO3.md`](docs/ESPECIFICACION_HITO3.md). Aquí, las
firmas.

## H3.1 Autenticación — `app/routers/auth.py` (prefix `/auth`)

- `POST /auth/login`: body `{"username", "password", "otp"?}`. Si el usuario
  tiene 2FA y falta `otp`: `401 otp_required`; si el código es incorrecto o
  ya se usó: `401 invalid_otp`. Tras `max_login_failures` (5) fallos
  seguidos: `429 too_many_attempts` durante `login_lockout_seconds` (300).
  `system` y `replicator` nunca pueden iniciar sesión.
- `GET /auth/2fa` → `{"username", "enabled"}`.
- `POST /auth/2fa/setup` → `{"secret", "otpauth_uri"}` (`409 2fa_already_enabled`).
- `POST /auth/2fa/enable` / `POST /auth/2fa/disable`: body `{"code"}` →
  `{"username", "enabled"}` (`400 invalid_otp`).
- `POST /auth/api-keys`: body `{"name", "expires_in_days"?}` → `201`
  `{"id", "name", "prefix", "key", "created_at", "expires_at", ...}`. `key`
  solo se devuelve aquí.
- `GET /auth/api-keys` → lista sin la clave. `DELETE /auth/api-keys/{id}` → revoca.
- Crear API keys y gestionar 2FA exige sesión con contraseña:
  `403 session_required` si se usa una API key.

Formas de autenticarse en el resto de la API (en este orden): `X-API-Key:
dfsha_...` o `Authorization: Bearer dfsha_...`; `Authorization: Bearer
<jwt>`; `X-Username` solo con `DFSHA_ALLOW_INSECURE_DEV_AUTH=true`.

## H3.2 Permisos y grupos — `app/routers/fs.py`, `app/routers/groups.py`

Todos los endpoints de `/fs` y `/files` comprueban permisos (app/acl.py);
sin permiso responden `403 permission_denied`. `ls` y `stat` devuelven
además `owner`, `group` y `mode` (`"drwxr-x---"`, con `+` si hay ACL;
`stat` también `mode_octal`).

- `POST /fs/chmod`: `{"path", "mode": "750"}` → `AclResponse`. Solo el dueño.
- `POST /fs/chgrp`: `{"path", "group": str | null}` → `AclResponse`. Dueño y miembro del grupo.
- `GET /fs/acl?path=` → `AclResponse`: `{"path", "owner", "group", "mode",
  "mode_octal", "entries": [{"type": "user"|"group", "name", "perms": "r-x"}]}`.
- `POST /fs/acl`: `{"path", "type", "name", "perms"}` añade o cambia una entrada. Solo el dueño.
- `DELETE /fs/acl`: `{"path", "type", "name"}` la quita. Solo el dueño.
- `POST /groups`: `{"name"}` → `201 GroupResponse` (`{"name", "owner",
  "members", "created_at"}`); quien lo crea es dueño y miembro. `409 already_exists`.
- `GET /groups` → grupos del usuario. `GET /groups/{name}`.
- `POST /groups/{name}/members`: `{"username"}`; `DELETE /groups/{name}/members/{username}`;
  `DELETE /groups/{name}` → `204`. Solo el dueño del grupo.

## H3.3 Cliente ↔ ControlNode: bloques y locks — `app/routers/control.py`

- `POST /files/{path}/allocate`: body `{"size_bytes", "block_size"?,
  "lease_id"?}`. Exige `w` sobre el archivo (o `w+x` en el padre si es
  nuevo). Toma un lease exclusivo (`409 file_locked` si otro lo tiene).
  Respuesta: `{"path", "file_id", "block_size", "total_size", "lease_id",
  "replication", "blocks": [{"index", "block_id", "file_id", "size_bytes",
  "datanode", "datanodes": [DataNodeLocation], "token", "lease_id"}]}`.
  `DataNodeLocation` = `{"node_id", "host", "port", "scheme"}` (dirección
  pública). El cliente sube a `datanodes[0]` con `Authorization: Bearer
  <token>`; si no responde, prueba el siguiente.
- `POST /files/{path}/blocks/{index}/allocate`: body `{"size_bytes",
  "lease_id"?}` → un `AllocatedBlock` (con cadena y token).
- `POST /files/{path}/confirm`: body `{"blocks": [{"block_id", "checksum",
  "size_bytes"}], "lease_id"?}`. Un bloque se confirma solo si al menos
  `min_replicas` DataNodes informaron ese checksum y tamaño; si no, `409
  replicas_not_ready` sin cambiar nada (reintentable). Libera el lease
  implícito al completar.
- `GET /files/{path}/blocks`: exige `r`. Cada bloque trae `datanodes` (réplicas
  `stored`, vivas primero) y un `token` de lectura.
- `POST /files/{path}/lock`: `{"mode": "exclusive"|"shared", "ttl_seconds"?}`
  → `201 LockResponse` (`{"lock_id", "path", "mode", "owner", "expires_at",
  "implicit"}`). `exclusive` exige `w`; `shared`, `r`. `409 file_locked` si choca.
- `POST /files/{path}/lock/{lock_id}/renew`: `{"ttl_seconds"?}`;
  `DELETE /files/{path}/lock/{lock_id}`; `GET /files/{path}/locks`.
  Un lock ajeno, vencido o de otro archivo: `409 invalid_lease`.
- `rm` y `rmdir -r` de un archivo bloqueado por otro usuario: `409 file_locked`.
- `GET /cluster/status` → `{"controlnode": {"controlnode_id", "role",
  "epoch", "leader_id", "applied_seq", "peers"}, "replication_factor",
  "datanodes": [{"node_id", "address", "status", "blocks"}], "files",
  "blocks", "under_replicated", "missing", "pending_replications",
  "pending_deletions"}`.

## H3.4 ControlNode ↔ DataNode — `app/routers/datanodes.py`

Todos exigen `Authorization: Bearer <token de nodo>` (firmado con
`DFSHA_CLUSTER_SECRET`, `typ=node`, `role=datanode`) cuyo `sub` sea el
`node_id` del cuerpo (`401 invalid_node_token`, `403 node_mismatch`).

- `POST /datanodes/register`: `{"node_id", "host", "port", "scheme",
  "internal_host"?, "internal_port"?}`. Solo en el líder.
- `POST /datanodes/heartbeat`: `{"node_id", "deleted_blocks": [str],
  "received_blocks": [ReceivedBlock], "corrupt_blocks": [str],
  "controlnodes": {controlnode_id: segundos_sin_contacto}}` → `{"node_id",
  "status", "last_heartbeat", "role", "epoch", "controlnode_id",
  "delete_blocks": [str], "replicate": [{"block_id", "file_id", "token",
  "targets": [DataNodeLocation interna]}]}`. Se manda a TODOS los
  ControlNodes; un respaldo solo anota el testigo y responde sin órdenes.
- `POST /datanodes/blocks/received`: `{"node_id", "blocks": [{"block_id",
  "checksum", "size_bytes"}]}` → `204`. Marca la réplica como `stored`.
- `POST /datanodes/block-report`: `{"node_id", "blocks": [...]}` → `{"known",
  "added", "lost", "orphans"}`.
- `GET /datanodes`: ahora exige usuario autenticado.

## H3.5 DataNode — `datanode/`

- `PUT /blocks/{block_id}?file_id=`: exige un token de bloque `op=write`
  del mismo bloque y archivo, con este DataNode dentro de `pipe` (`401`/`403`).
  `X-Checksum-Sha256` opcional (`400 checksum_mismatch`). Un bloque ya
  guardado con otro contenido: `409 block_exists`; con el mismo, no-op.
  Tras guardar, informa al líder (`/datanodes/blocks/received`) y reenvía
  al siguiente nodo de `pipe`. Respuesta: `{"block_id", "file_id",
  "size_bytes", "checksum", "stored_on": [node_id], "failed": [node_id]}`.
- `GET /blocks/{block_id}`: exige token `op=read` del mismo bloque.
- `DELETE /blocks/{block_id}`, `DELETE /files/{file_id}/blocks`: exigen un
  token de nodo con `role=controlnode` (administración).
- Con `DFSHA_DATANODE_STORAGE_KEY`, los bloques se guardan cifrados con
  AES-256-GCM; los escritos antes sin cifrar se siguen leyendo.
- Variables nuevas: `CONTROLNODE_URLS`, `DATANODE_INTERNAL_HOST/PORT`,
  `CLUSTER_SECRET`, `DATANODE_STORAGE_KEY`, `TLS_CERT`, `TLS_KEY`, `CA_CERT`,
  `BLOCK_REPORT_INTERVAL`. `JWT_SECRET` ya no se usa.

## H3.6 ControlNode ↔ ControlNode — `app/ha/router.py` (prefix `/ha`)

- `GET /ha/status` (público) → `{"controlnode_id", "role": "leader"|"standby",
  "epoch", "leader_id", "applied_seq", "public_url", "ha_enabled"}`.
- `GET /ha/log?since=N&wait=S` (token de nodo `role=controlnode`): long-poll,
  entradas con `seq > N` → `{"epoch", "leader_id", "last_seq", "entries":
  [{"seq", "epoch", "table", "op": "upsert"|"delete"|"snapshot", "row"}]}`.
  `since` es el acuse del respaldo. `409 not_leader` en un respaldo; `410
  snapshot_required` si el log ya se podó o el respaldo va por delante.
- `GET /ha/snapshot` (token de ControlNode) → `{"seq", "epoch", "leader_id",
  "tables": {nombre: [filas]}}`.
- Un respaldo responde `503 not_leader` a todo lo demás, salvo `/health`,
  `/ha/*` y `/datanodes/heartbeat`.

## H3.7 Cliente — `cli/`

- `dfsha login <u> -u <url1>,<url2> --ca-cert certs/ca.crt`, con `--api-key` o
  pidiendo el código 2FA si hace falta. Cada petición va al líder y cambia
  sola de ControlNode tras un failover.
- `put`: sube cada bloque una vez al primero de su cadena (prueba el
  siguiente si falla), reintenta `confirm` mientras llegan los informes y
  renueva el lease; `--lock <id>` para escribir con un lock explícito.
- `get`: lee de la primera réplica que funcione.
- Comandos nuevos: `status`, `chmod`, `chgrp`, `acl get|set|rm`,
  `group create|add|remove|list|delete`, `lock`, `unlock`, `locks`,
  `2fa status|setup|enable|disable`, `apikey create|list|revoke`.

## H3.8 Infraestructura

`docker-compose.yml`: `controlnode-1` (primario, :8000), `controlnode-2`
(respaldo, :8010), `datanode-1..4` (:8001–8004). Todo con TLS (certificados
por nodo en `certs/`) y secretos en `.env`, ambos generados por
`scripts/setup_cluster.py` y fuera de git. `scripts/verify_hito3.py` verifica
el hito con caídas reales.

---

## Resumen de lo que YA existe (no reimplementar)

| Módulo | Contenido |
|---|---|
| `app/models.py` | `User`, `Directory`, `File`, `DataNode`, `Block`, `BlockReplica`, `PendingDeletion`, `ReplicationTask`, `FileLock`, `Group`, `GroupMember`, `AclEntry`, `ApiKey`, `HALogEntry`, `HAMeta` |
| `app/database.py` | `engine`, `SessionLocal`, `get_db` |
| `app/deps.py` | `get_current_user` (API key, JWT, X-Username de desarrollo), `get_session_user` |
| `app/path_service.py` | Resolución/validación de rutas, excepciones |
| `app/acl.py` | Permisos efectivos y comprobaciones (`require_*`) |
| `app/locks.py` | Locks y leases de escritura |
| `app/placement.py` | Liveness de DataNodes, partición y cadenas de réplica |
| `app/replication.py` | Informes, re-replicación, sobrantes, conciliación |
| `app/block_gc.py` | Cola de borrado de bloques (`discard_blocks`, `discard_replica`) |
| `app/cluster_auth.py` | Tokens de nodo y de bloque |
| `app/ha/` | Log de cambios, `/ha/*`, failover y middleware |
| `app/schemas.py` | Todos los modelos Pydantic mencionados arriba |
| `app/main.py` | App FastAPI, `/health`, migraciones, seed, bucles de HA y re-replicación |
| `datanode/` | Servicio DataNode |
| `cli/dfsha_cli/distributed.py` | put/get por bloques con réplicas |

**Regla para código nuevo:** toda escritura de metadatos debe pasar por el
ORM (`db.add`, `db.delete`, asignar atributos). Un `delete()`/`update()` de
Core no dispara los eventos que alimentan la réplica entre ControlNodes y no
se replicaría.
