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
    owner: str | None = Field(default=None, description="username del dueño")
    group: str | None = Field(default=None, description="grupo dueño, si tiene")
    mode: str | None = Field(default=None, description="permisos estilo ls -l, p. ej. 'drwxr-x---'")


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
    group: str | None = None
    mode: str | None = Field(default=None, description="permisos estilo ls -l")
    mode_octal: str | None = Field(default=None, description="permisos en octal, p. ej. '0750'")


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
    otp: str | None = Field(default=None, description="Código 2FA de 6 dígitos, si el usuario activó 2FA")


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
    scheme: str = Field(default="http", pattern="^https?$")
    internal_host: str | None = Field(
        default=None, max_length=255, description="Host con el que lo alcanzan los otros nodos"
    )
    internal_port: int | None = Field(default=None, ge=1, le=65535)


class ReceivedBlock(BaseModel):
    """Un bloque que un DataNode acaba de guardar, con el checksum que él
    mismo calculó."""

    block_id: str
    checksum: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)


class HeartbeatRequest(BaseModel):
    """Cuerpo de POST /datanodes/heartbeat."""

    node_id: str = Field(min_length=1, max_length=128)
    received_blocks: list[ReceivedBlock] = Field(
        default_factory=list,
        description="Informes de bloques guardados que no se pudieron enviar al momento",
    )
    corrupt_blocks: list[str] = Field(
        default_factory=list, description="block_ids cuyo contenido en disco no cuadra con su checksum"
    )
    controlnodes: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Testigo para el failover: segundos desde el último contacto OK con cada "
            "ControlNode (por controlnode_id)"
        ),
    )
    deleted_blocks: list[str] = Field(
        default_factory=list,
        description=(
            "Acuse de recibo: block_ids que el DataNode ya borró de su disco "
            "(de órdenes `delete_blocks` de heartbeats anteriores)"
        ),
    )


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
    delete_blocks: list[str] = Field(
        default_factory=list,
        description=(
            "Órdenes de borrado: block_ids que el DataNode debe eliminar de su "
            "disco (el namespace ya no los referencia). Debe acusarlos en el "
            "siguiente heartbeat vía `deleted_blocks`."
        ),
    )
    replicate: list["ReplicateCommand"] = Field(
        default_factory=list, description="Órdenes de copiar bloques a otros DataNodes"
    )
    role: str = Field(default="leader", description="Rol de este ControlNode: leader | standby")
    epoch: int = 1
    controlnode_id: str = ""


class DataNodeLocation(BaseModel):
    """Dónde vive un bloque: lo mínimo que el cliente necesita para hablar
    con el DataNode (PUT/GET /blocks/{block_id})."""

    node_id: str
    host: str
    port: int
    scheme: str = "http"


class ReplicateCommand(BaseModel):
    """Orden para un DataNode: copiar su réplica de `block_id` a `targets`
    (re-replicación). `token` es un token de escritura para el destino."""

    block_id: str
    file_id: int
    token: str
    targets: list[DataNodeLocation] = Field(description="Direcciones internas de los destinos")


class BlocksReceivedRequest(BaseModel):
    """POST /datanodes/blocks/received: el DataNode informa que guardó bloques."""

    node_id: str
    blocks: list[ReceivedBlock]


class BlockReportRequest(BaseModel):
    """POST /datanodes/block-report: inventario completo de un DataNode, para
    reconciliar réplicas perdidas o huérfanas."""

    node_id: str
    blocks: list[ReceivedBlock]


class BlockReportResponse(BaseModel):
    known: int
    added: int
    lost: int
    orphans: int


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
    lease_id: str | None = Field(default=None, description="Lock exclusivo que ya tiene el cliente, si lo tomó antes")


class AllocatedBlock(BaseModel):
    """Una entrada del plan de escritura: dónde subir un bloque concreto."""

    index: int = Field(description="Orden del bloque dentro del archivo (0-based)")
    block_id: str = Field(description="Id opaco con el que subir el bloque al DataNode")
    file_id: int = Field(
        description=(
            "Id del archivo dueño del bloque. El cliente lo manda como "
            "?file_id= al subir (PUT {datanode}/blocks/{block_id}?file_id=...): "
            "el DataNode organiza su storage como un directorio por archivo "
            "y un archivo por bloque dentro de él."
        )
    )
    size_bytes: int = Field(description="Bytes que debe tener este bloque")
    datanode: DataNodeLocation = Field(description="Primer nodo de la cadena (a él sube el cliente)")
    datanodes: list[DataNodeLocation] = Field(
        default_factory=list,
        description="Cadena de réplica completa, en orden. Si el primero no responde, el cliente prueba el siguiente",
    )
    token: str = Field(default="", description="Token de escritura del bloque (Authorization en el DataNode)")
    lease_id: str | None = Field(default=None, description="Lease de escritura que hay que mandar en confirm")


class BlockAllocateRequest(BaseModel):
    """Cuerpo de POST /files/{path}/blocks/{index}/allocate: escritura CRUD
    de UN bloque puntual (no de todo el archivo). Se usa para actualizar un
    bloque existente (índice < número de bloques actuales) o para anexar
    uno nuevo al final (índice == número de bloques actuales)."""

    size_bytes: int = Field(ge=0, description="Tamaño del nuevo contenido de este bloque")
    lease_id: str | None = Field(default=None, description="Lease o lock exclusivo que ya tiene el cliente")


class AllocateResponse(BaseModel):
    """Respuesta de POST /files/{path}/allocate: el plan de escritura
    completo. El cliente sube cada bloque a su DataNode y luego llama a
    /confirm."""

    path: str = Field(description="Ruta absoluta normalizada del archivo")
    file_id: int
    block_size: int = Field(description="Tamaño de bloque efectivamente usado")
    total_size: int
    blocks: list[AllocatedBlock]
    lease_id: str | None = Field(default=None, description="Lease de escritura exclusivo (mandarlo en confirm)")
    replication: int = Field(default=1, description="Réplicas por bloque en este plan")


class ConfirmBlock(BaseModel):
    """Confirmación de un bloque ya subido: su checksum y tamaño reales."""

    block_id: str
    checksum: str = Field(min_length=1, max_length=64, description="SHA-256 hex del bloque subido")
    size_bytes: int = Field(ge=0)


class ConfirmRequest(BaseModel):
    """Cuerpo de POST /files/{path}/confirm: los bloques que se subieron OK."""

    blocks: list[ConfirmBlock]
    lease_id: str | None = Field(default=None, description="Lease de escritura devuelto por allocate")


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
    file_id: int = Field(description="Id del archivo dueño del bloque (ver AllocatedBlock)")
    size_bytes: int
    checksum: str | None = Field(description="SHA-256 hex esperado del bloque")
    datanode: DataNodeLocation = Field(description="Réplica preferida (la primera de `datanodes`)")
    datanodes: list[DataNodeLocation] = Field(
        default_factory=list, description="Réplicas disponibles, vivas primero; el cliente prueba en orden"
    )
    token: str = Field(default="", description="Token de lectura del bloque")


class BlocksResponse(BaseModel):
    """Respuesta de GET /files/{path}/blocks: el plan de lectura ordenado.
    El cliente descarga cada bloque de su DataNode, verifica el checksum y
    reensambla en orden de `index`."""

    path: str
    file_id: int
    size_bytes: int
    num_blocks: int
    blocks: list[BlockPlacement]


# ==========================================================================
# Contrato Hito 3 — Seguridad, consistencia y alta disponibilidad
# ==========================================================================
class TwoFactorSetupResponse(BaseModel):
    secret: str = Field(description="Secreto TOTP en base32 (para escribirlo en la app de autenticación)")
    otpauth_uri: str = Field(description="URI otpauth:// (se puede convertir en QR)")


class TwoFactorCodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class TwoFactorStatusResponse(BaseModel):
    username: str
    enabled: bool


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyInfo(BaseModel):
    id: int
    name: str
    prefix: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked: bool


class ApiKeyCreateResponse(ApiKeyInfo):
    key: str = Field(description="La clave completa. Solo se muestra esta vez")


class GroupCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")


class GroupMemberRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)


class GroupResponse(BaseModel):
    name: str
    owner: str
    members: list[str]
    created_at: datetime


class ChmodRequest(BaseModel):
    path: str
    mode: str = Field(description="Octal, p. ej. '750' o '1777'")


class ChgrpRequest(BaseModel):
    path: str
    group: str | None = Field(description="Nombre del grupo, o null para quitarlo")


class AclEntryModel(BaseModel):
    type: str = Field(pattern="^(user|group)$")
    name: str
    perms: str = Field(description="'rwx', 'r-x', ...")


class AclResponse(BaseModel):
    path: str
    owner: str
    group: str | None
    mode: str
    mode_octal: str
    entries: list[AclEntryModel]


class AclSetRequest(BaseModel):
    path: str
    type: str = Field(pattern="^(user|group)$")
    name: str
    perms: str


class AclRemoveRequest(BaseModel):
    path: str
    type: str = Field(pattern="^(user|group)$")
    name: str


class LockRequest(BaseModel):
    mode: str = Field(default="exclusive", pattern="^(exclusive|shared)$")
    ttl_seconds: int | None = Field(default=None, ge=1)


class LockResponse(BaseModel):
    lock_id: str
    path: str
    mode: str
    owner: str
    expires_at: datetime
    implicit: bool = False


class ClusterNodeStatus(BaseModel):
    node_id: str
    address: str
    status: str
    blocks: int


class ControlNodeStatus(BaseModel):
    controlnode_id: str
    role: str
    epoch: int
    leader_id: str | None
    applied_seq: int
    peers: list[dict] = Field(default_factory=list)


class ClusterStatusResponse(BaseModel):
    controlnode: ControlNodeStatus
    replication_factor: int
    datanodes: list[ClusterNodeStatus]
    files: int
    blocks: int
    under_replicated: int
    missing: int
    pending_replications: int
    pending_deletions: int


# HeartbeatResponse referencia a ReplicateCommand, definido después.
HeartbeatResponse.model_rebuild()
