# Contratos de API — DFSha (Hito 1)

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

Tablas nuevas en `app/models.py`: `datanodes` (host, puerto, estado,
último heartbeat) y `blocks` (block_id, file_id, índice, tamaño, checksum,
datanode_id, committed).

## 4. DataNode ↔ ControlNode — `app/routers/datanodes.py` (prefix `/datanodes`)

**Sin autenticación de usuario** (tráfico interno nodo↔control; el
aseguramiento nodo-a-nodo es del Hito 3).

### `POST /datanodes/register`

Registra un DataNode al arrancar; idempotente por `node_id` (si ya existe,
actualiza host/puerto y lo reactiva). Cuenta como un heartbeat.

- Body: `DataNodeRegisterRequest` (`{"node_id": str, "host": str, "port": int}`).
- Respuesta `200`: `DataNodeInfo`.

### `POST /datanodes/heartbeat`

Refresca el heartbeat de un DataNode ya registrado.

- Body: `HeartbeatRequest` (`{"node_id": str}`).
- Respuesta `200`: `HeartbeatResponse`.
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
  `file_id`, nuevos `block_id`).
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
- `index < len(blocks) actuales`: reemplaza ese bloque (nuevo `block_id`,
  posiblemente otro DataNode; el contenido viejo queda huérfano en su
  DataNode anterior, igual que en el re-allocate completo).
- `index == len(blocks) actuales`: anexa un bloque al final.
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
- Respuesta `200`: `ConfirmResponse` (`complete=true` si se confirmaron
  todos los bloques planificados).
- Errores: `404 not_found` (el archivo no existe), `409 not_a_file`,
  `400 unknown_block` (un `block_id` no pertenece a ese archivo).

### `GET /files/{path}/blocks` — plan de lectura

Devuelve, en orden por `index`, de qué DataNode bajar cada bloque
**confirmado** y con qué checksum verificarlo. El cliente descarga
(`GET {datanode}/blocks/{block_id}`), verifica y reensambla.

- Respuesta `200`: `BlocksResponse` — `blocks: [BlockPlacement]`.
- Un archivo sin bloques confirmados devuelve `blocks: []`.
- Errores: `404 not_found`, `409 not_a_file`.

## 6. Servicio DataNode — `datanode/` (Jacobo)

**Estado: implementado.** Servicio FastAPI nuevo e independiente del
ControlNode, sin autenticación de usuario (tráfico interno):

- `PUT /blocks/{block_id}?file_id=...` — sube/reemplaza un bloque. Guarda
  el contenido en `<storage_dir>/<file_id>/<block_id>` (decisión "archivo
  como directorio, bloque como archivo") y devuelve tamaño + checksum
  SHA-256 real.
- `GET /blocks/{block_id}` — descarga un bloque (404 si no existe).
- `DELETE /blocks/{block_id}` — borra un bloque suelto.
- `DELETE /files/{file_id}/blocks` — borra TODOS los bloques de un
  archivo de un golpe; la usa RF1 para propagar `rm` / `rmdir --recursive`
  a los DataNodes que tengan bloques de los archivos borrados.
- `GET /health`.

Al arrancar (y cada `DFSHA_HEARTBEAT_INTERVAL` segundos) se registra y
late contra el ControlNode (`app/datanode/registration.py`), reintentando
solo si falla — así puede arrancar antes que el ControlNode sin caerse.

Variables de entorno (`datanode/app/config.py`, prefijo `DFSHA_`):
`DATANODE_ID`, `CONTROLNODE_URL`, `DATANODE_ADVERTISE_HOST`,
`DATANODE_ADVERTISE_PORT` (también el puerto en el que escucha uvicorn,
ver `datanode/Dockerfile`), `HEARTBEAT_INTERVAL`, `DATANODE_STORAGE_DIR`.

## Infraestructura

`docker-compose.yml` (raíz) levanta 1 ControlNode + 3 DataNodes en la red
`dfsha_net`, cada uno con su propio `DFSHA_DATANODE_ID` y
`DFSHA_DATANODE_ADVERTISE_HOST/PORT` apuntando al nombre de su servicio.

---

## Resumen de lo que YA existe (no reimplementar)

| Módulo | Contenido |
|---|---|
| `app/models.py` | `User`, `Directory`, `File` (SQLAlchemy) |
| `app/database.py` | `engine`, `SessionLocal`, `get_db` |
| `app/deps.py` | `get_current_user` |
| `app/path_service.py` | Resolución/validación de rutas, excepciones |
| `app/schemas.py` | Todos los modelos Pydantic mencionados arriba |
| `app/main.py` | App FastAPI, `/health`, seed de la raíz |
