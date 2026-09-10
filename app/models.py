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
    pueda tocar los archivos de otro— es del Hito 3: aquí `owner_id` registra
    al dueño pero el namespace sigue siendo global.
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

    owner: Mapped["User"] = relationship(back_populates="directories")
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

    owner: Mapped["User"] = relationship(back_populates="files")
    parent: Mapped["Directory"] = relationship(back_populates="files")
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

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DataNode node_id={self.node_id!r} {self.host}:{self.port} status={self.status}>"


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
    """

    __tablename__ = "blocks"
    __table_args__ = (
        UniqueConstraint("file_id", "block_index", name="uq_block_file_index"),
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
    datanode: Mapped["DataNode"] = relationship(back_populates="blocks")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Block block_id={self.block_id!r} file_id={self.file_id} idx={self.block_index}>"
