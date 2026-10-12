"""
Fixtures compartidas para los tests. Usa SQLite en memoria para no tocar
ningún archivo del disco ni depender de estado entre tests.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base
from app.ha.state import state as ha_state
from app.models import User
from app.path_service import get_or_create_root
from app.routers.auth import login_throttle


@pytest.fixture(autouse=True)
def _test_settings(monkeypatch):
    """Ajustes comunes de los tests:
    - X-Username permitido (los tests de módulos lo usan para no hacer login).
    - Sin periodo de gracia en los borrados (las órdenes salen al momento).
    - ControlNode único (HA apagado) y contador de logins limpio.
    """
    monkeypatch.setattr(settings, "allow_insecure_dev_auth", True)
    monkeypatch.setattr(settings, "deletion_grace_seconds", 0.0)
    ha_state.reset()
    login_throttle.reset()
    yield
    ha_state.reset()


@pytest.fixture()
def db() -> Session:
    """Sesión de BD contra una SQLite en memoria, con tablas creadas y
    un usuario + directorio raíz ya sembrados, lista para usarse en cada
    test de forma aislada (se recrea desde cero en cada test).

    poolclass=StaticPool es necesario para que la BD en memoria se
    comparta entre hilos: TestClient ejecuta las peticiones en otro
    hilo, y sin esto cada hilo vería su propia base vacía.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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

@pytest.fixture()
def api(db, tmp_path, monkeypatch):
    """La API completa del ControlNode (todos los routers, sin lifespan)
    sobre la BD en memoria. Sin usuario por defecto: cada test elige con
    `as_user(nombre)`."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import placement
    from app.database import get_db
    from app.routers import auth, cluster, control, datanodes, fs, groups, transfer

    placement.reset_cursor()
    monkeypatch.setattr(transfer, "STORAGE_DIR", tmp_path)
    app = FastAPI()
    app.include_router(auth.router, prefix="/auth")
    app.include_router(fs.router, prefix="/fs")
    app.include_router(groups.router, prefix="/groups")
    app.include_router(cluster.router, prefix="/cluster")
    app.include_router(datanodes.router, prefix="/datanodes")
    app.include_router(control.router, prefix="/files")
    app.include_router(transfer.router, prefix="/files")
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def as_user(name: str) -> dict[str, str]:
    return {"X-Username": name}
