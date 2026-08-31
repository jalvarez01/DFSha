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

    Nota: en el Hito 1 (monolítico) no se implementa autenticación real;
    este modelo existe para que `owner_id` tenga sentido desde ya y los
    compañeros no tengan que migrar el esquema cuando se agregue login.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
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

    def __repr__(self) -> str:  # pragma: no cover
        return f"<File id={self.id} name={self.name!r} parent_id={self.parent_id}>"
