"""
Configuración de SQLAlchemy: engine, sesiones y la dependencia de FastAPI
que el resto de módulos (los de mis compañeros incluidos) deben usar para
obtener una sesión de base de datos por request.
"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# `check_same_thread` se necesita solo para SQLite: por defecto SQLite no
# permite compartir una conexión entre threads distintos, pero uvicorn
# atiende cada request potencialmente en threads diferentes.
_connect_args = (
    {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
)

engine = create_engine(settings.database_url, connect_args=_connect_args)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Clase base declarativa de la que heredan todos los modelos ORM."""


def get_db() -> Generator[Session, None, None]:
    """
    Dependencia de FastAPI: entrega una sesión de BD por request y
    garantiza que se cierre al terminar, incluso si hubo una excepción.

    Uso en un endpoint (de cualquier compañero):

        @router.get("/algo")
        def algo(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
