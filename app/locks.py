"""
Locks de archivo y leases de escritura (Hito 3: consistencia, RF3 lock()).

Problema que resuelven: sin coordinación, dos clientes que escriben el
mismo archivo a la vez se pisan (cada `allocate` descarta el plan del otro
y el `confirm` del que llega tarde deja un archivo mezclado).

- Toda escritura (allocate, allocate de un bloque, confirm) necesita un
  lock EXCLUSIVO sobre el archivo. Si el cliente no trae uno (`lease_id`),
  allocate toma un *lease* implícito que confirm libera al completar.
- Un cliente puede tomar locks explícitos con POST /files/{ruta}/lock:
  `exclusive` (nadie más escribe ni toma locks) o `shared` (varios a la vez;
  impide que otros escriban). Con un lock explícito, las escrituras deben
  pasar su id como `lease_id`, y el lock sigue vigente tras confirmar.
- Todos vencen (`expires_at`) y se pueden renovar: un cliente que muere no
  deja el archivo bloqueado para siempre.
- Las lecturas no necesitan lock: el plan de lectura solo incluye bloques
  confirmados, que son inmutables, así que un lector siempre ve una versión
  completa del archivo aunque alguien esté escribiendo.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import File, FileLock, User

EXCLUSIVE = "exclusive"
SHARED = "shared"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


class LockConflict(HTTPException):
    def __init__(self, message: str) -> None:
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail={"error": "file_locked", "message": message})


class InvalidLease(HTTPException):
    def __init__(self, message: str) -> None:
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail={"error": "invalid_lease", "message": message})


def is_active(lock: FileLock, now: datetime | None = None) -> bool:
    return _as_utc(lock.expires_at) > (now or _now())


def active_locks(db: Session, file_row: File) -> list[FileLock]:
    now = _now()
    return [lock for lock in file_row.locks if is_active(lock, now)]


def _purge_expired(db: Session, file_row: File) -> None:
    now = _now()
    for lock in list(file_row.locks):
        if not is_active(lock, now):
            db.delete(lock)
    db.flush()
    db.expire(file_row, ["locks"])


def _describe(lock: FileLock) -> str:
    return f"lock {lock.mode} de {lock.owner.username} hasta {_as_utc(lock.expires_at):%H:%M:%S} UTC"


def acquire(
    db: Session, file_row: File, user: User, mode: str, ttl_seconds: int | None = None, implicit: bool = False
) -> FileLock:
    """Toma un lock (no hace commit). Lanza LockConflict si choca."""
    _purge_expired(db, file_row)
    for other in active_locks(db, file_row):
        if mode == EXCLUSIVE or other.mode == EXCLUSIVE:
            raise LockConflict(f"El archivo está bloqueado ({_describe(other)})")
    ttl = min(ttl_seconds or settings.lease_ttl_seconds, settings.max_lock_ttl_seconds)
    lock = FileLock(
        lock_id=uuid.uuid4().hex,
        file_id=file_row.id,
        owner_id=user.id,
        mode=mode,
        implicit=implicit,
        expires_at=_now() + timedelta(seconds=ttl),
    )
    db.add(lock)
    db.flush()
    db.expire(file_row, ["locks"])
    return lock


def get_owned_lock(db: Session, file_row: File, user: User, lock_id: str) -> FileLock:
    lock = db.scalar(select(FileLock).where(FileLock.lock_id == lock_id))
    if lock is None or lock.file_id != file_row.id or not is_active(lock):
        raise InvalidLease(f"El lock {lock_id!r} no existe, venció o no es de este archivo")
    if lock.owner_id != user.id:
        raise InvalidLease(f"El lock {lock_id!r} es de otro usuario")
    return lock


def renew(lock: FileLock, ttl_seconds: int | None = None) -> None:
    ttl = min(ttl_seconds or settings.lease_ttl_seconds, settings.max_lock_ttl_seconds)
    lock.expires_at = _now() + timedelta(seconds=ttl)


def writer_lock(db: Session, file_row: File, user: User, lease_id: str | None) -> FileLock:
    """El lock exclusivo con el que se hace una escritura: el que trae el
    cliente (renovado) o un lease implícito nuevo."""
    if lease_id:
        lock = get_owned_lock(db, file_row, user, lease_id)
        if lock.mode != EXCLUSIVE:
            raise InvalidLease("Para escribir hace falta un lock exclusivo, no compartido")
        renew(lock)
        return lock
    return acquire(db, file_row, user, EXCLUSIVE, implicit=True)


def release(db: Session, lock: FileLock) -> None:
    db.delete(lock)
    db.flush()


def assert_not_locked_by_others(db: Session, file_row: File, user: User) -> None:
    """Para rm/rmdir: no se borra un archivo que otro tiene bloqueado."""
    for lock in active_locks(db, file_row):
        if lock.owner_id != user.id:
            raise LockConflict(f"No se puede borrar {file_row.name}: {_describe(lock)}")
