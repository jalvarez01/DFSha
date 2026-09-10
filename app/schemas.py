"""
Modelos Pydantic (request/response) que forman el CONTRATO de la API.

Estos modelos son la parte "de datos" del contrato descrito en
CONTRATOS.md (en la raíz del repo). Los defino yo (dueño del modelo de
datos) para que mis compañeros puedan importar directamente estas clases
en sus routers y no tengan que inventar (ni ponerse de acuerdo) sobre la
forma exacta de cada JSON.

Organización:
- Modelos genéricos (entradas de directorio, errores).
- Modelos para el compañero de FS (ls/mkdir/rmdir/rm).
- Modelos para el compañero de transferencia (put/get).

Ninguno de estos modelos implica los endpoints en sí (eso es trabajo de
cada compañero); solo fija las formas de entrada/salida.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class EntryType(str, Enum):
    """Tipo de entrada al listar un directorio."""

    DIRECTORY = "directory"
    FILE = "file"


class ErrorResponse(BaseModel):
    """Forma estándar de error para toda la API. Todos los endpoints,
    de cualquier compañero, deben devolver este cuerpo en sus respuestas
    de error (4xx/5xx), vía HTTPException(detail=ErrorResponse(...).model_dump())
    o un exception handler global."""

    error: str = Field(description="Código corto y estable, p. ej. 'not_found'")
    message: str = Field(description="Mensaje legible para humanos, en español")


# --------------------------------------------------------------------------
# Contrato: endpoints de gestión de FS (ls / mkdir / rmdir / rm)
# --------------------------------------------------------------------------
class DirectoryEntry(BaseModel):
    """Una entrada dentro de un listado de directorio (`ls`)."""

    name: str
    type: EntryType
    size_bytes: int = Field(description="0 para directorios")
    created_at: datetime


class ListDirectoryResponse(BaseModel):
    """Respuesta de GET /fs/ls."""

    path: str = Field(description="Ruta absoluta normalizada que fue listada")
    entries: list[DirectoryEntry]


class MkdirRequest(BaseModel):
    """Cuerpo de POST /fs/mkdir."""

    path: str = Field(description="Ruta absoluta o relativa al cwd del directorio a crear")
    parents: bool = Field(
        default=False,
        description=(
            "Equivalente a 'mkdir -p': crea los directorios intermedios que falten y no "
            "falla si el destino ya existe. Con False, el padre debe existir ya"
        ),
    )


class DirectoryResponse(BaseModel):
    """Respuesta de POST /fs/mkdir (y útil para cualquier endpoint que
    devuelva info de un directorio)."""

    id: int
    path: str = Field(description="Ruta absoluta normalizada")
    name: str
    created_at: datetime


class RmdirRequest(BaseModel):
    """Cuerpo de DELETE /fs/rmdir."""

    path: str
    recursive: bool = Field(
        default=False,
        description="Si es False y el directorio no está vacío, la operación debe fallar (409)",
    )


class RmRequest(BaseModel):
    """Cuerpo de DELETE /fs/rm (borra un archivo)."""

    path: str


class DeleteResponse(BaseModel):
    """Respuesta genérica para rmdir/rm exitosos."""

    path: str
    deleted: bool = True


class StatResponse(BaseModel):
    """Respuesta de GET /fs/stat: metadatos de una entrada cualquiera del
    árbol, sea archivo o directorio. Es lo que permite a la CLI validar un
    `cd` sin tener que listar el directorio entero."""

    path: str = Field(description="Ruta absoluta normalizada")
    name: str
    type: EntryType
    size_bytes: int = Field(description="0 para directorios")
    owner: str = Field(description="username del dueño")
    created_at: datetime
    updated_at: datetime | None = Field(
        default=None, description="None para directorios (no registran modificación)"
    )


# --------------------------------------------------------------------------
# Contrato: endpoints de transferencia (put / get)
# --------------------------------------------------------------------------
class FileMetadata(BaseModel):
    """Metadatos de un archivo, tal como quedan registrados en la tabla
    `files`. Es lo que debe devolver PUT tras subir un archivo, y lo que
    debe exponer un endpoint de "stat"/HEAD antes de un GET."""

    id: int
    path: str = Field(description="Ruta absoluta normalizada")
    name: str
    size_bytes: int
    owner: str = Field(description="username del dueño")
    created_at: datetime
    updated_at: datetime


class PutFileResponse(BaseModel):
    """Respuesta de subir un archivo (PUT /files/{path})."""

    file: FileMetadata
    created: bool = Field(description="True si el archivo no existía antes, False si se sobreescribió")


# --------------------------------------------------------------------------
# Usuarios (soporte mínimo para que owner_id tenga sentido en el Hito 1)
# --------------------------------------------------------------------------
class UserResponse(BaseModel):
    id: int
    username: str
    created_at: datetime


# --------------------------------------------------------------------------
# Contrato: autenticación (POST /auth/login)
# --------------------------------------------------------------------------
class LoginRequest(BaseModel):
    """Credenciales de inicio de sesión."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    """JWT emitido tras un login correcto. El cliente lo manda de vuelta en
    cada petición como `Authorization: Bearer <access_token>`."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Segundos de validez del token")
    username: str


# ==========================================================================
# Contrato Hito 2 — ControlNode (DFS distribuido por bloques)
# ==========================================================================
# Estos modelos fijan la forma exacta de los cuerpos que intercambian:
#   - DataNode  <-> ControlNode : register / heartbeat
#   - Cliente   <-> ControlNode : allocate (plan de escritura) / confirm /
#                                 blocks (plan de lectura)
# El cliente (Paulina/Mariana) usa DataNodeLocation para saber a qué
# host:puerto subir/bajar cada bloque, sin que el ControlNode toque datos.


class DataNodeStatus(str, Enum):
    """Último estado reportado por un DataNode."""

    ALIVE = "alive"
    DEAD = "dead"


# --- DataNode <-> ControlNode ---------------------------------------------
class DataNodeRegisterRequest(BaseModel):
    """Cuerpo de POST /datanodes/register. Lo manda el DataNode al arrancar."""

    node_id: str = Field(min_length=1, max_length=128, description="Id estable elegido por el DataNode")
    host: str = Field(min_length=1, max_length=255, description="Host/IP alcanzable por el cliente")
    port: int = Field(ge=1, le=65535)


class HeartbeatRequest(BaseModel):
    """Cuerpo de POST /datanodes/heartbeat."""

    node_id: str = Field(min_length=1, max_length=128)


class DataNodeInfo(BaseModel):
    """Vista completa de un DataNode (respuesta de register y de GET
    /datanodes). `status` refleja la liveness *calculada* al momento de la
    consulta, no solo el último valor persistido."""

    node_id: str
    host: str
    port: int
    status: DataNodeStatus
    last_heartbeat: datetime | None
    registered_at: datetime


class HeartbeatResponse(BaseModel):
    """Respuesta de POST /datanodes/heartbeat: acuse con el estado guardado."""

    node_id: str
    status: DataNodeStatus
    last_heartbeat: datetime


class DataNodeLocation(BaseModel):
    """Dónde vive un bloque: lo mínimo que el cliente necesita para hablar
    con el DataNode (PUT/GET /blocks/{block_id})."""

    node_id: str
    host: str
    port: int


# --- Cliente <-> ControlNode: plan de escritura (allocate + confirm) ------
class AllocateRequest(BaseModel):
    """Cuerpo de POST /files/{path}/allocate. El cliente ya conoce el tamaño
    total del archivo (lo tiene en disco) antes de subir nada."""

    size_bytes: int = Field(ge=0, description="Tamaño total del archivo a subir")
    block_size: int | None = Field(
        default=None,
        ge=1,
        description="Tamaño de bloque deseado; si es None se usa settings.block_size_bytes",
    )


class AllocatedBlock(BaseModel):
    """Una entrada del plan de escritura: dónde subir un bloque concreto."""

    index: int = Field(description="Orden del bloque dentro del archivo (0-based)")
    block_id: str = Field(description="Id opaco con el que subir el bloque al DataNode")
    size_bytes: int = Field(description="Bytes que debe tener este bloque")
    datanode: DataNodeLocation


class AllocateResponse(BaseModel):
    """Respuesta de POST /files/{path}/allocate: el plan de escritura
    completo. El cliente sube cada bloque a su DataNode y luego llama a
    /confirm."""

    path: str = Field(description="Ruta absoluta normalizada del archivo")
    file_id: int
    block_size: int = Field(description="Tamaño de bloque efectivamente usado")
    total_size: int
    blocks: list[AllocatedBlock]


class ConfirmBlock(BaseModel):
    """Confirmación de un bloque ya subido: su checksum y tamaño reales."""

    block_id: str
    checksum: str = Field(min_length=1, max_length=64, description="SHA-256 hex del bloque subido")
    size_bytes: int = Field(ge=0)


class ConfirmRequest(BaseModel):
    """Cuerpo de POST /files/{path}/confirm: los bloques que se subieron OK."""

    blocks: list[ConfirmBlock]


class ConfirmResponse(BaseModel):
    """Respuesta de POST /files/{path}/confirm."""

    path: str
    file_id: int
    size_bytes: int = Field(description="Tamaño total consolidado (suma de bloques confirmados)")
    num_blocks: int = Field(description="Bloques confirmados")
    complete: bool = Field(description="True si todos los bloques planificados quedaron confirmados")


# --- Cliente <-> ControlNode: plan de lectura -----------------------------
class BlockPlacement(BaseModel):
    """Una entrada del plan de lectura: de dónde bajar un bloque y con qué
    checksum verificarlo."""

    index: int
    block_id: str
    size_bytes: int
    checksum: str | None = Field(description="SHA-256 hex esperado del bloque")
    datanode: DataNodeLocation


class BlocksResponse(BaseModel):
    """Respuesta de GET /files/{path}/blocks: el plan de lectura ordenado.
    El cliente descarga cada bloque de su DataNode, verifica el checksum y
    reensambla en orden de `index`."""

    path: str
    file_id: int
    size_bytes: int
    num_blocks: int
    blocks: list[BlockPlacement]
