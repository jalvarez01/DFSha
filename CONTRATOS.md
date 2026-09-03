# Contratos de API — DFSha (Hito 1)

Este documento fija la firma exacta de los endpoints que **no** implemento
yo, para que cada compañero pueda empezar a codear en paralelo sin
esperar a que el resto avance. Los modelos Pydantic mencionados ya están
definidos en `app/schemas.py`; impórtenlos directamente, no los
reescriban.

Todos los endpoints deben:
- Recibir `db: Session = Depends(get_db)` (de `app.database`).
- Recibir `user: User = Depends(get_current_user)` (de `app.deps`) para
  saber quién es el dueño de la operación (header `X-Username`).
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

Fuera de esa tabla, la app devuelve además `401 unauthenticated` (falta el
header `X-Username`, ver `app/deps.py`) y `422 validation_error` (cuerpo de
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

## Resumen de lo que YA existe (no reimplementar)

| Módulo | Contenido |
|---|---|
| `app/models.py` | `User`, `Directory`, `File` (SQLAlchemy) |
| `app/database.py` | `engine`, `SessionLocal`, `get_db` |
| `app/deps.py` | `get_current_user` |
| `app/path_service.py` | Resolución/validación de rutas, excepciones |
| `app/schemas.py` | Todos los modelos Pydantic mencionados arriba |
| `app/main.py` | App FastAPI, `/health`, seed de la raíz |
