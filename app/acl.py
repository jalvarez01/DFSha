"""
Control de acceso a archivos y directorios (Hito 3, RNF6).

Modelo POSIX simplificado:

- Cada archivo/directorio tiene dueño (`owner_id`), grupo opcional
  (`group_id`) y bits de modo: rwx para dueño, grupo y otros (0o777), más el
  *sticky bit* (0o1000) en directorios.
- Además puede tener entradas de ACL (`acl_entries`) que dan permisos rwx a
  un usuario o grupo concreto.

Permisos efectivos de un usuario sobre una entrada (orden POSIX):

1. Superusuario (`settings.superusers`): todo.
2. Dueño: los bits de dueño.
3. Entrada de ACL de usuario para él: esos permisos.
4. Si es miembro del grupo de la entrada o de algún grupo con entrada de
   ACL: la unión de esos permisos.
5. Si no: los bits de "otros".

Qué exige cada operación (como en Linux):

- Atravesar un directorio (llegar a algo que está dentro): `x` en él y en
  todos sus ancestros.
- Listar un directorio: `r` en él.
- Crear o borrar una entrada: `w` y `x` en el directorio padre. Si el padre
  tiene sticky bit (la raíz `/` es 1777, como /tmp), solo el dueño de la
  entrada o del padre puede borrarla.
- Leer un archivo: `r`. Escribirlo (reemplazar contenido): `w`.
- chmod, chgrp y ACL: solo el dueño.

Por defecto lo que crea un usuario es privado (0o700 / 0o600: "cada cliente
gestiona solo sus propios archivos") y hereda el grupo del directorio padre.
"""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Directory, File, GroupMember, User

R, W, X = 4, 2, 1
STICKY = 0o1000


class PermissionDenied(HTTPException):
    """403 permission_denied. Es un HTTPException para que cualquier router
    pueda dejarlo propagar sin traducirlo."""

    def __init__(self, message: str) -> None:
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "permission_denied", "message": message},
        )
        self.message = message

    def __str__(self) -> str:
        return self.message


def is_superuser(user: User) -> bool:
    return user.username in settings.superuser_names


def user_group_ids(db: Session, user: User) -> set[int]:
    return set(db.scalars(select(GroupMember.group_id).where(GroupMember.user_id == user.id)))


def effective_perms(db: Session, user: User, node: Directory | File, groups: set[int] | None = None) -> int:
    if is_superuser(user):
        return R | W | X
    if node.owner_id == user.id:
        return (node.mode >> 6) & 7

    entries = list(node.acl_entries)
    for entry in entries:
        if entry.user_id == user.id:
            return entry.perms

    groups = user_group_ids(db, user) if groups is None else groups
    matched = False
    perms = 0
    if node.group_id is not None and node.group_id in groups:
        perms |= (node.mode >> 3) & 7
        matched = True
    for entry in entries:
        if entry.group_id is not None and entry.group_id in groups:
            perms |= entry.perms
            matched = True
    if matched:
        return perms
    return node.mode & 7


def _describe(node: Directory | File) -> str:
    kind = "directorio" if isinstance(node, Directory) else "archivo"
    return f"{kind} {node.name or '/'}"


def require(db: Session, user: User, node: Directory | File, needed: int, what: str) -> None:
    if effective_perms(db, user, node) & needed != needed:
        raise PermissionDenied(f"Permiso denegado: {what} ({_describe(node)})")


def require_traverse(db: Session, user: User, directory: Directory) -> None:
    """`x` en `directory` y en todos sus ancestros."""
    if is_superuser(user):
        return
    groups = user_group_ids(db, user)
    current: Directory | None = directory
    while current is not None:
        if effective_perms(db, user, current, groups) & X != X:
            raise PermissionDenied(
                f"Permiso denegado: no puedes atravesar el {_describe(current)}"
            )
        current = current.parent


def require_create(db: Session, user: User, parent: Directory) -> None:
    require_traverse(db, user, parent)
    require(db, user, parent, W | X, "no puedes crear entradas aquí")


def require_delete(db: Session, user: User, parent: Directory, node: Directory | File) -> None:
    require_traverse(db, user, parent)
    require(db, user, parent, W | X, "no puedes borrar entradas aquí")
    if parent.mode & STICKY and not is_superuser(user):
        if user.id not in (node.owner_id, parent.owner_id):
            raise PermissionDenied(
                f"Permiso denegado: el directorio tiene sticky bit y el {_describe(node)} no es tuyo"
            )


def require_read_file(db: Session, user: User, file_row: File) -> None:
    require_traverse(db, user, file_row.parent)
    require(db, user, file_row, R, "no puedes leer el archivo")


def require_write_file(db: Session, user: User, file_row: File) -> None:
    require_traverse(db, user, file_row.parent)
    require(db, user, file_row, W, "no puedes escribir el archivo")


def require_owner(user: User, node: Directory | File) -> None:
    if node.owner_id != user.id and not is_superuser(user):
        raise PermissionDenied(f"Permiso denegado: solo el dueño puede cambiar los permisos del {_describe(node)}")


def apply_creation_defaults(node: Directory | File, parent: Directory) -> None:
    """Modo por defecto (privado) y grupo heredado del padre."""
    node.mode = settings.default_dir_mode if isinstance(node, Directory) else settings.default_file_mode
    node.group_id = parent.group_id


def mode_string(node: Directory | File) -> str:
    """'drwxr-x---' al estilo ls -l (con 't'/'T' para el sticky bit)."""
    out = ["d" if isinstance(node, Directory) else "-"]
    for shift in (6, 3, 0):
        bits = (node.mode >> shift) & 7
        out.append("r" if bits & R else "-")
        out.append("w" if bits & W else "-")
        out.append("x" if bits & X else "-")
    if node.mode & STICKY:
        out[-1] = "t" if out[-1] == "x" else "T"
    if node.acl_entries:
        out.append("+")
    return "".join(out)


def perms_string(perms: int) -> str:
    return ("r" if perms & R else "-") + ("w" if perms & W else "-") + ("x" if perms & X else "-")


def parse_perms(text: str) -> int:
    """'rw-', 'rx', '6' -> bits."""
    text = text.strip()
    if text.isdigit() and len(text) == 1 and int(text) <= 7:
        return int(text)
    bits = 0
    for char in text:
        if char == "r":
            bits |= R
        elif char == "w":
            bits |= W
        elif char == "x":
            bits |= X
        elif char != "-":
            raise ValueError(f"Permisos inválidos: {text!r} (usa algo como 'rwx', 'r-x' o un dígito 0-7)")
    return bits
