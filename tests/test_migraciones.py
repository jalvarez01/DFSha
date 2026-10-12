"""
Migración de BD de hitos anteriores al esquema del Hito 3: columnas nuevas,
raíz con sticky bit y réplicas para los bloques que ya existían.
"""
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app import main as main_module
from app.models import Block, BlockReplica, Directory

# Esquema de las tablas tal como las dejaba el Hito 2 (sin modo, grupo,
# esquema de los DataNodes, etc.).
_HITO2_DDL = [
    "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64) NOT NULL UNIQUE, password_hash VARCHAR(255), created_at DATETIME)",
    "CREATE TABLE directories (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, parent_id INTEGER, owner_id INTEGER NOT NULL, created_at DATETIME, CONSTRAINT uq_directory_parent_name UNIQUE (parent_id, name))",
    "CREATE TABLE files (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, parent_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, size_bytes BIGINT NOT NULL, created_at DATETIME, updated_at DATETIME, CONSTRAINT uq_file_parent_name UNIQUE (parent_id, name))",
    "CREATE TABLE datanodes (id INTEGER PRIMARY KEY, node_id VARCHAR(128) NOT NULL UNIQUE, host VARCHAR(255) NOT NULL, port INTEGER NOT NULL, status VARCHAR(16) NOT NULL, last_heartbeat DATETIME NOT NULL, registered_at DATETIME NOT NULL)",
    "CREATE TABLE blocks (id INTEGER PRIMARY KEY, block_id VARCHAR(64) NOT NULL UNIQUE, file_id INTEGER NOT NULL, block_index INTEGER NOT NULL, size_bytes BIGINT NOT NULL, checksum VARCHAR(64), datanode_id INTEGER NOT NULL, committed BOOLEAN NOT NULL, created_at DATETIME NOT NULL, CONSTRAINT uq_block_file_index_state UNIQUE (file_id, block_index, committed))",
    "CREATE TABLE pending_deletions (id INTEGER PRIMARY KEY, datanode_id INTEGER NOT NULL, block_id VARCHAR(64) NOT NULL, file_id INTEGER NOT NULL, created_at DATETIME, CONSTRAINT uq_pending_deletion UNIQUE (datanode_id, block_id))",
]
_HITO2_DATA = [
    "INSERT INTO users VALUES (1, 'system', NULL, '2026-09-01 00:00:00')",
    "INSERT INTO users VALUES (2, 'ana', 'x', '2026-09-01 00:00:00')",
    "INSERT INTO directories VALUES (1, '', NULL, 1, '2026-09-01 00:00:00')",
    "INSERT INTO files VALUES (1, 'viejo.bin', 1, 2, 10, '2026-09-01 00:00:00', '2026-09-01 00:00:00')",
    "INSERT INTO datanodes VALUES (1, 'dn-0', 'h', 9000, 'alive', '2026-09-01 00:00:00', '2026-09-01 00:00:00')",
    "INSERT INTO blocks VALUES (1, 'b-viejo', 1, 0, 10, 'abc', 1, 1, '2026-09-01 00:00:00')",
    "INSERT INTO pending_deletions VALUES (1, 1, 'b-borrado', 9, '2026-09-01 00:00:00')",
]


def test_una_bd_del_hito_2_queda_al_dia(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'hito2.db'}")
    with engine.begin() as conn:
        for statement in _HITO2_DDL + _HITO2_DATA:
            conn.execute(text(statement))

    monkeypatch.setattr(main_module, "engine", engine)
    monkeypatch.setattr(main_module, "SessionLocal", sessionmaker(bind=engine))
    added = main_module.prepare_database()
    main_module.seed_and_migrate_data(added)

    columns = {c["name"] for c in inspect(engine).get_columns("files")}
    assert {"mode", "group_id"} <= columns
    assert ("datanodes", "scheme") in added
    assert "block_replicas" in inspect(engine).get_table_names()

    db = sessionmaker(bind=engine)()
    # Lo existente queda legible (0644/0755) y la raíz pasa a 1777.
    assert db.query(Directory).filter(Directory.parent_id.is_(None)).one().mode == 0o1777
    with engine.connect() as conn:
        assert conn.execute(text("SELECT mode FROM files")).scalar() == 0o644
        assert conn.execute(text("SELECT scheme FROM datanodes")).scalar() == "http"
        # La orden de borrado vieja se puede entregar ya.
        assert conn.execute(text("SELECT not_before FROM pending_deletions")).scalar().startswith("1970")
    # El bloque viejo tiene su réplica en el DataNode donde estaba.
    replica = db.query(BlockReplica).one()
    assert (replica.datanode_id, replica.state, replica.checksum) == (1, "stored", "abc")
    assert db.query(Block).one().replicas == [replica]

    # Idempotente: una segunda pasada no añade nada.
    assert main_module.prepare_database() == set()
    main_module.seed_and_migrate_data(set())
    assert db.query(BlockReplica).count() == 1
