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

| Excepción                        | Código HTTP |
|-----------------------------------|-------------|
| `InvalidPathError`                 | 400 |
| `NotFoundError`                    | 404 |
| `NotADirectoryError` / `NotAFileError` | 409 |
| `AlreadyExistsError`               | 409 |
| `RootOperationError`               | 400 |

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

## 1. Compañero de FS: `ls` / `mkdir` / `rmdir` / `rm`

Archivo sugerido: `app/routers/fs.py`, registrado en `main.py` con
`prefix="/fs"`.

### `GET /fs/ls`

Lista el contenido de un directorio.

- Query params: `path: str` (default `"/"`), `cwd_id: int | None = None`.
- Usa `path_service.resolve_directory` + `path_service.list_children`.
- Respuesta `200`: `ListDirectoryResponse` (`app.schemas`).
- Errores: `404` si el directorio no existe, `409` si `path` apunta a un
  archivo.

### `POST /fs/mkdir`

Crea un directorio nuevo (no recursivo: el padre debe existir ya).

- Body: `MkdirRequest` (`{"path": "..."}`).
- Usa `path_service.resolve_parent_and_name`, luego verifica con
  `find_child_directory`/`find_child_file` si el nombre ya existe →
  `AlreadyExistsError` (409) si sí. Si no existe, crea el `Directory` con
  `owner_id=user.id`.
- Respuesta `201`: `DirectoryResponse`.
- Errores: `404` (padre no existe), `409` (ya existe algo con ese nombre),
  `400` (ruta inválida).

### `DELETE /fs/rmdir`

Borra un directorio.

- Body: `RmdirRequest` (`{"path": "...", "recursive": false}`).
- Usa `path_service.resolve_directory`. Si `recursive=False` y el
  directorio tiene hijos (usar `list_children`), responder `409` sin
  borrar nada. Si `recursive=True`, borrar (el `cascade` del modelo se
  encarga de los descendientes).
- No debe permitirse borrar la raíz (`RootOperationError` → 400; en la
  práctica `resolve_directory("/")` da el root, chequeen
  `directory.is_root` antes de borrar).
- Respuesta `200`: `DeleteResponse`.

### `DELETE /fs/rm`

Borra un archivo.

- Body: `RmRequest` (`{"path": "..."}`).
- Usa `path_service.resolve_file`.
- Respuesta `200`: `DeleteResponse`.
- Errores: `404` (no existe), `409` (`path` es un directorio → usar rmdir).

---

## 2. Compañero de transferencia: `put` / `get`

Archivo sugerido: `app/routers/transfer.py`, registrado en `main.py` con
`prefix="/files"`.

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
- Respuesta: `StreamingResponse`/`FileResponse` con el contenido, o un
  endpoint separado `GET /files/{path:path}/meta` que devuelva
  `FileMetadata` si prefieren separar metadata de contenido.
- Errores: `404` (no existe), `409` (`path` es un directorio).

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
