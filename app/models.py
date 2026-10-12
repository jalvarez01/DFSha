"""
Modelo de datos (ORM) de DFSha.

Jerarquía tipo sistema de archivos Linux:

    users (dueños de archivos/directorios)
    directories (árbol, autorreferenciado por parent_id)
        └── files (hojas del árbol, viven dentro de un directory)

El directorio raíz ("/") es una fila especial en `directories` con
`parent_id = NULL` y `name = ""`. Todo lo demás cuelga de ahí, así que
cualquier ruta absoluta se resuelve caminando desde esa raíz.

Restricciones de integridad importantes:
- (parent_id, name) es único tanto en directories como en files: dentro
  de un mismo directorio no puede haber dos hijos con el mismo nombre
  (igual que en un FS real, aunque aquí SÍ permitimos que un archivo y un
  subdirectorio compartan nombre entre tablas distintas... lo evitamos
  a nivel de servicio, ver app/path_service.py, para simplificar el
  modelo y no forzar una tabla polimórfica).
- Borrado en cascada: si se borra un directorio, sus subdirectorios y
  archivos se borran con él (a nivel de BD). La política de "solo permitir
  rmdir si está vacío" es una decisión de negocio y le corresponde
  implementarla al compañero de endpoints FS, no al modelo.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    """Timestamp UTC consistente para toda la app (evita depender de la
    zona horaria del servidor)."""
    return datetime.now(timezone.utc)


class User(Base):
    """Usuario dueño de archivos y directorios.

    Autenticación: `password_hash` guarda la contraseña hasheada y
    POST /auth/login la verifica para emitir un JWT (ver app/routers/auth.py
    y app/security.py). El control de acceso por usuario —que un usuario no
    pueda tocar los archivos de otro— lo aplica app/acl.py desde el Hito 3
    con dueño, grupo, bits de modo y ACL.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    # Hash PBKDF2 de la contraseña (ver app/security.py). Es nullable porque
    # existen usuarios sin contraseña: el 'system' que crea la raíz al
    # arrancar, y los que autocrea el header X-Username. Un usuario sin
    # hash no puede iniciar sesión hasta que un login le fije contraseña.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # 2FA (TOTP, RFC 6238). `totp_secret` se fija en /auth/2fa/setup y solo
    # se exige en el login cuando `totp_enabled`; `totp_last_counter` evita
    # reutilizar el mismo código dos veces (anti-replay).
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    totp_last_counter: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )

    directories: Mapped[list["Directory"]] = relationship(back_populates="owner")
    files: Mapped[list["File"]] = relationship(back_populates="owner")

    def __repr__(self) -> str:  # pragma: no cover - solo para debug
        return f"<User id={self.id} username={self.username!r}>"


class Directory(Base):
    """Nodo del árbol de directorios (incluye la raíz, con parent_id=None)."""

    __tablename__ = "directories"
    __table_args__ = (
        UniqueConstraint("parent_id", "name", name="uq_directory_parent_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("directories.id", ondelete="CASCADE"), nullable=True, index=True
    )
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Control de acceso (Hito 3, ver app/acl.py): grupo dueño y bits de modo
    # estilo Unix (rwx para dueño/grupo/otros, más el sticky bit 0o1000).
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True
    )
    mode: Mapped[int] = mapped_column(Integer, default=0o755, server_default="493", nullable=False)

    owner: Mapped["User"] = relationship(back_populates="directories")
    group: Mapped["Group | None"] = relationship()
    acl_entries: Mapped[list["AclEntry"]] = relationship(
        back_populates="directory", cascade="all, delete-orphan"
    )
    parent: Mapped["Directory | None"] = relationship(
        remote_side=[id], back_populates="children"
    )
    children: Mapped[list["Directory"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    files: Mapped[list["File"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Directory id={self.id} name={self.name!r} parent_id={self.parent_id}>"


class File(Base):
    """Archivo (hoja del árbol). Guarda solo metadatos: el contenido/bloques
    los maneja el compañero de transferencia (put/get), típicamente en un
    almacenamiento de bloques aparte; este modelo es la fuente de verdad
    del "namespace" (nombre, ubicación, tamaño, dueño)."""

    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("parent_id", "name", name="uq_file_parent_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[int] = mapped_column(
        ForeignKey("directories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True
    )
    mode: Mapped[int] = mapped_column(Integer, default=0o644, server_default="420", nullable=False)

    owner: Mapped["User"] = relationship(back_populates="files")
    parent: Mapped["Directory"] = relationship(back_populates="files")
    group: Mapped["Group | None"] = relationship()
    acl_entries: Mapped[list["AclEntry"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )
    locks: Mapped[list["FileLock"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )
    # Bloques en que se particiona este archivo (Hito 2, DFS distribuido).
    # En el Hito 1 monolítico esta lista queda vacía: el contenido vive como
    # un blob único en disco (ver app/routers/transfer.py). Con la escritura
    # distribuida, cada archivo pasa a tener N filas en `blocks`, repartidas
    # entre DataNodes. El cascade garantiza que borrar el archivo (rm/rmdir)
    # elimine también sus filas de metadatos de bloque; la propagación del
    # borrado al DataNode que guarda el bloque real es trabajo aparte (RF1
    # distribuido) usando esta misma información.
    blocks: Mapped[list["Block"]] = relationship(
        back_populates="file",
        cascade="all, delete-orphan",
        order_by="Block.block_index",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<File id={self.id} name={self.name!r} parent_id={self.parent_id}>"


class DataNode(Base):
    """Un servidor de bloques registrado contra el ControlNode.

    El ControlNode (esta app, en su rol de "NameNode" tipo HDFS) no guarda
    bloques: solo sabe *qué* DataNodes existen, *dónde* están (host:puerto)
    y si están *vivos*, para poder repartir bloques entre ellos y devolver
    a los clientes el plan de lectura/escritura.

    Ciclo de vida:
    - `POST /datanodes/register` inserta o reactiva la fila al arrancar el
      DataNode (idempotente por `node_id`).
    - `POST /datanodes/heartbeat` refresca `last_heartbeat` periódicamente.
    - Se considera "vivo" si `status == 'alive'` y su último heartbeat cae
      dentro de `settings.heartbeat_ttl_seconds` (ver app/placement.py).
      La liveness es dinámica (no un flag estático): un nodo que deja de
      latir se excluye del reparto aunque su fila siga en `status='alive'`.
    """

    __tablename__ = "datanodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Identificador estable que el propio DataNode elige y repite en cada
    # register/heartbeat (p. ej. "datanode-1"). Es la clave de negocio: si
    # un DataNode se reinicia, vuelve a registrarse con el mismo node_id y
    # se reutiliza su fila en vez de duplicarla.
    node_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    # "http" o "https" (Hito 3: TLS).
    scheme: Mapped[str] = mapped_column(String(8), default="http", server_default="http", nullable=False)
    # Dirección con la que lo alcanzan los OTROS nodos (cadena de escritura y
    # re-replicación). En docker-compose `host` es localhost (para la CLI del
    # anfitrión) pero entre contenedores hace falta el nombre del servicio.
    internal_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    internal_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # "alive" | "dead". Es solo el último estado reportado; la liveness real
    # combina esto con la antigüedad de last_heartbeat.
    status: Mapped[str] = mapped_column(String(16), default="alive", nullable=False)
    last_heartbeat: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    blocks: Mapped[list["Block"]] = relationship(back_populates="datanode")

    @property
    def peer_host(self) -> str:
        return self.internal_host or self.host

    @property
    def peer_port(self) -> int:
        return self.internal_port or self.port

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DataNode node_id={self.node_id!r} {self.host}:{self.port} status={self.status}>"


# Nombre de la restricción de unicidad de `blocks`. Versiones anteriores
# usaban "uq_block_file_index" (file_id, block_index); app/main.py migra las
# BD de desarrollo que todavía la tengan (ver _migrate_blocks_table).
BLOCKS_UNIQUE_CONSTRAINT = "uq_block_file_index_state"


class Block(Base):
    """Un bloque de un archivo, almacenado en un DataNode concreto.

    Es la unidad de particionamiento del DFS (RNF4): un archivo grande se
    parte en bloques de tamaño fijo (`settings.block_size_bytes`, 8 MiB por
    defecto) y cada bloque se asigna a un DataNode vía round-robin sobre los
    nodos vivos (ver app/placement.py).

    Esta tabla es *solo metadatos*: el contenido del bloque vive en el disco
    del DataNode, indexado por `block_id`. El ControlNode guarda a qué
    archivo pertenece (`file_id`), su orden dentro del archivo
    (`block_index`), su tamaño, su checksum SHA-256 y en qué DataNode está
    (`datanode_id`).

    Flujo de dos fases de la escritura:
    - `allocate` crea las filas con `committed=False` y `checksum=NULL`
      (todavía no se ha subido nada).
    - El cliente sube cada bloque a su DataNode y luego `confirm` fija el
      checksum real y marca `committed=True`.
    El plan de lectura (`GET .../blocks`) devuelve solo los bloques ya
    confirmados, que son los que de verdad se pueden descargar.

    Reemplazo de un bloque (`POST .../blocks/{index}/allocate`): mientras el
    reemplazo no se confirma, conviven DOS filas con el mismo índice, la
    vieja confirmada (la que se sigue leyendo) y la nueva pendiente. Por eso
    la unicidad es por (file_id, block_index, committed): como mucho una
    confirmada y una pendiente por índice. Al confirmar la nueva, `confirm`
    borra la vieja y encola su borrado en el DataNode (PendingDeletion).
    """

    __tablename__ = "blocks"
    __table_args__ = (
        UniqueConstraint("file_id", "block_index", "committed", name=BLOCKS_UNIQUE_CONSTRAINT),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Identificador global y opaco del bloque (UUID hex). Es la clave con la
    # que el cliente y el DataNode se refieren al bloque: PUT/GET/DELETE
    # /blocks/{block_id} en la API del DataNode.
    block_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Orden del bloque dentro del archivo (0-based). El cliente reensambla
    # concatenando los bloques por este índice.
    block_index: Mapped[int] = mapped_column(Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # SHA-256 hex del contenido del bloque. NULL hasta que `confirm` lo fija.
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    datanode_id: Mapped[int] = mapped_column(
        ForeignKey("datanodes.id"), nullable=False, index=True
    )
    committed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    file: Mapped["File"] = relationship(back_populates="blocks")
    # Desde el Hito 3, `datanode` es solo el nodo PRIMARIO de la cadena de
    # escritura (al que el cliente sube). Dónde está el bloque de verdad lo
    # dice `replicas`.
    datanode: Mapped["DataNode"] = relationship(back_populates="blocks")
    replicas: Mapped[list["BlockReplica"]] = relationship(
        back_populates="block", cascade="all, delete-orphan", order_by="BlockReplica.id"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Block block_id={self.block_id!r} file_id={self.file_id} idx={self.block_index}>"


class PendingDeletion(Base):
    """Bloque que un DataNode debe borrar de su disco.

    Cuando el namespace deja de referenciar un bloque (rm, rmdir -r,
    re-allocate, reemplazo de un bloque, PUT heredado sobre un archivo
    distribuido), su fila en `blocks` se borra en la MISMA transacción en la
    que se encola aquí la orden de borrado físico. El ControlNode no llama
    al DataNode: igual que el NameNode de HDFS, entrega las órdenes en la
    respuesta del heartbeat (`delete_blocks`), y el DataNode acusa recibo en
    el heartbeat siguiente (`deleted_blocks`), momento en que la fila se
    elimina. Entrega al-menos-una-vez: borrar un bloque es idempotente.

    Así el borrado distribuido no depende de que el ControlNode alcance al
    DataNode por red (en docker-compose el host que se anuncia a los
    clientes no tiene por qué ser alcanzable desde el ControlNode) y
    sobrevive a DataNodes caídos: la orden espera hasta que el nodo vuelva.
    """

    __tablename__ = "pending_deletions"
    __table_args__ = (
        UniqueConstraint("datanode_id", "block_id", name="uq_pending_deletion"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    datanode_id: Mapped[int] = mapped_column(
        ForeignKey("datanodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    block_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Sin FK: el archivo normalmente ya no existe cuando el DataNode borra.
    file_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # No se entrega antes de esta hora: periodo de gracia para lectores que
    # obtuvieron el plan justo antes de que el bloque saliera del namespace.
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default="1970-01-01 00:00:00", nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PendingDeletion block_id={self.block_id!r} datanode_id={self.datanode_id}>"


class BlockReplica(Base):
    """Una copia de un bloque en un DataNode (Hito 3, replicación).

    Estados:
    - `pending`: asignada (cadena de escritura o re-replicación), todavía
      sin confirmar por el DataNode.
    - `stored`: el DataNode informó que la guardó (POST
      /datanodes/blocks/received), con el checksum que él mismo calculó.

    El ControlNode nunca se fía del cliente para saber dónde quedó un
    bloque: solo cuentan los informes de los DataNodes.
    """

    __tablename__ = "block_replicas"
    __table_args__ = (UniqueConstraint("block_pk", "datanode_id", name="uq_block_replica"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    block_pk: Mapped[int] = mapped_column(
        ForeignKey("blocks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    datanode_id: Mapped[int] = mapped_column(
        ForeignKey("datanodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    block: Mapped["Block"] = relationship(back_populates="replicas")
    datanode: Mapped["DataNode"] = relationship()


class ReplicationTask(Base):
    """Orden de copiar un bloque de `source` a `target` para recuperar su
    factor de replicación (alta disponibilidad). Se entrega al DataNode
    origen en la respuesta de su heartbeat; se cumple cuando el destino
    informa que guardó el bloque, y se reprograma si vence."""

    __tablename__ = "replication_tasks"
    __table_args__ = (UniqueConstraint("block_pk", "target_id", name="uq_replication_task"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    block_pk: Mapped[int] = mapped_column(
        ForeignKey("blocks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("datanodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_id: Mapped[int] = mapped_column(
        ForeignKey("datanodes.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    block: Mapped["Block"] = relationship()
    source: Mapped["DataNode"] = relationship(foreign_keys=[source_id])
    target: Mapped["DataNode"] = relationship(foreign_keys=[target_id])


class FileLock(Base):
    """Lock sobre un archivo (RF3 lock(), consistencia ante escrituras
    concurrentes).

    - `exclusive`: un solo dueño; nadie más puede escribir ni tomar otro
      lock. Las escrituras (allocate/confirm) toman uno implícito
      (`implicit=True`, un *lease*) que se libera al confirmar.
    - `shared`: varios lectores; impide que otros escriban mientras dure.

    Todos vencen (`expires_at`): un cliente que muere no deja el archivo
    bloqueado para siempre.
    """

    __tablename__ = "file_locks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lock_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    implicit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    file: Mapped["File"] = relationship(back_populates="locks")
    owner: Mapped["User"] = relationship()


class Group(Base):
    """Grupo de usuarios para compartir archivos (Hito 3, control de acceso).
    Lo crea cualquier usuario, que queda como dueño y puede añadir o quitar
    miembros."""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    owner: Mapped["User"] = relationship()
    members: Mapped[list["GroupMember"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_group_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    group: Mapped["Group"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship()


class AclEntry(Base):
    """Entrada de ACL: permisos rwx (bits 4/2/1) para un usuario o grupo
    concreto sobre un archivo o directorio, además de los bits de modo.
    Exactamente uno de `directory_id`/`file_id` y uno de
    `user_id`/`group_id` va informado."""

    __tablename__ = "acl_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    directory_id: Mapped[int | None] = mapped_column(
        ForeignKey("directories.id", ondelete="CASCADE"), nullable=True, index=True
    )
    file_id: Mapped[int | None] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), nullable=True, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=True
    )
    perms: Mapped[int] = mapped_column(Integer, nullable=False)

    directory: Mapped["Directory | None"] = relationship(back_populates="acl_entries")
    file: Mapped["File | None"] = relationship(back_populates="acl_entries")
    user: Mapped["User | None"] = relationship()
    group: Mapped["Group | None"] = relationship()


class ApiKey(Base):
    """API key de un usuario (Hito 3): alternativa al login interactivo para
    scripts. Solo se guarda el SHA-256 de la clave; la clave completa se
    muestra una única vez al crearla."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped["User"] = relationship()


# --------------------------------------------------------------------------
# Alta disponibilidad del ControlNode (Hito 3). Estas dos tablas son LOCALES
# de cada ControlNode: no se replican (ver app/ha/).
# --------------------------------------------------------------------------
class HALogEntry(Base):
    """Log de cambios de metadatos que el líder genera en la misma
    transacción que cada escritura y que los respaldos aplican en orden."""

    __tablename__ = "ha_log"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    table_name: Mapped[str] = mapped_column(String(64), nullable=False)
    op: Mapped[str] = mapped_column(String(8), nullable=False)
    row: Mapped[str] = mapped_column(Text, nullable=False)


class HAMeta(Base):
    """Estado persistente de HA de este nodo (p. ej. la época)."""

    __tablename__ = "ha_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)


# Tablas que no viajan por el log de replicación.
LOCAL_TABLES = frozenset({"ha_log", "ha_meta"})
