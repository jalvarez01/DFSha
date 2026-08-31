"""
Punto de entrada de la aplicación FastAPI.

Este archivo SOLO se encarga de:
- Crear las tablas (create_all) y sembrar el directorio raíz al arrancar.
- Exponer /health.
- Registrar los routers de mis compañeros (comentado por ahora, ver abajo).

Los endpoints de negocio (ls/mkdir/rmdir/rm, put/get) NO se implementan
aquí: cada compañero debe crear su propio `APIRouter` en un módulo nuevo
(p. ej. app/routers/fs.py, app/routers/transfer.py) e incluirlo abajo con
`app.include_router(...)`, usando los modelos de app/schemas.py y las
funciones de app/path_service.py.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import User
from app.path_service import get_or_create_root


def _seed_root_directory() -> None:
    """Crea las tablas si no existen y garantiza que exista:
    - un usuario 'system' (dueño inicial de la raíz), y
    - el directorio raíz ("/").

    Se ejecuta una sola vez al arrancar el servidor.
    """
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        system_user = db.scalar(select(User).where(User.username == "system"))
        if system_user is None:
            system_user = User(username="system")
            db.add(system_user)
            db.commit()
            db.refresh(system_user)

        get_or_create_root(db, owner_id=system_user.id)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _seed_root_directory()
    yield
    # Nada que limpiar al apagar por ahora (SQLite no requiere pool teardown).


app = FastAPI(
    title=settings.app_name,
    description="DFS por bloques tipo HDFS — Hito 1 (monolítico cliente/servidor)",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["infra"])
def health() -> dict[str, str]:
    """Endpoint de salud. Útil para healthchecks de Docker/orquestador."""
    return {"status": "ok", "service": settings.app_name}


# ---------------------------------------------------------------------
# Routers de mis compañeros.
#
# from app.routers.fs import router as fs_router
# app.include_router(fs_router, prefix="/fs", tags=["fs"])
# ---------------------------------------------------------------------
from app.routers.transfer import router as transfer_router

app.include_router(transfer_router, prefix="/files", tags=["transfer"])
