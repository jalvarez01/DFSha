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
