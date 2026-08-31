"""
Fixtures compartidas para los tests. Usa SQLite en memoria para no tocar
ningún archivo del disco ni depender de estado entre tests.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import User
from app.path_service import get_or_create_root


@pytest.fixture()
def db() -> Session:
    """Sesión de BD contra una SQLite en memoria, con tablas creadas y
    un usuario + directorio raíz ya sembrados, lista para usarse en cada
    test de forma aislada (se recrea desde cero en cada test)."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    user = User(username="tester")
    session.add(user)
    session.commit()
    session.refresh(user)

    get_or_create_root(session, owner_id=user.id)

    yield session

    session.close()
    engine.dispose()


@pytest.fixture()
def owner_id(db: Session) -> int:
    """Id del usuario 'tester' sembrado por la fixture `db`."""
    return db.query(User).filter_by(username="tester").one().id
