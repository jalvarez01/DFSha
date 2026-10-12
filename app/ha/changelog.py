"""
Log de cambios de metadatos para replicar el ControlNode (ControlNode <->
ControlNode).

Captura (en el líder)
---------------------
Se escuchan los eventos `after_insert`, `after_update` y `after_delete` de
TODOS los modelos (salvo `ha_log` y `ha_meta`). Cada fila escrita genera una
entrada en `ha_log` con la fila completa en JSON, insertada por la MISMA
conexión y dentro de la MISMA transacción que la escritura: si la
transacción se deshace, la entrada también. Así cualquier router queda
replicado sin tener que acordarse de nada.

Como SQLite serializa a los escritores (una transacción de escritura a la
vez), el orden de `seq` coincide con el orden de commit: un respaldo que
aplica las entradas en orden de `seq` reconstruye exactamente el estado del
líder.

Requisito para quien escriba código nuevo: todas las escrituras deben pasar
por el ORM (`db.add`, `db.delete`, asignar atributos). Un `delete()` o
`update()` de Core no dispara estos eventos y no se replicaría.

Aplicación (en el respaldo)
---------------------------
`apply_entries` reaplica cada entrada (INSERT OR REPLACE / DELETE por clave
primaria) y copia la entrada a su propio `ha_log` con el mismo `seq`, para
que, si este nodo se promueve, siga la numeración y pueda servir el log.

Snapshot
--------
`export_snapshot` / `import_snapshot`: copia completa de todas las tablas
replicadas, para un respaldo nuevo, uno que se quedó atrás (el log se poda)
o un antiguo líder que vuelve y debe descartar lo que no llegó a replicar.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import DateTime, Engine, event, func, insert, select
from sqlalchemy.engine import Connection

from app.database import Base
from app.ha.state import current_write, state
from app.models import LOCAL_TABLES, HALogEntry

_LOG = HALogEntry.__table__
UPSERT = "upsert"
DELETE = "delete"
SNAPSHOT_MARKER = "snapshot"


def replicated_tables():
    """Tablas replicadas, en orden de dependencias (padres primero)."""
    return [table for table in Base.metadata.sorted_tables if table.name not in LOCAL_TABLES]


def _serialize(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_dict(mapper, target) -> dict:
    return {column.name: _serialize(getattr(target, prop.key)) for prop in mapper.column_attrs for column in prop.columns}


def _record(connection: Connection, mapper, target, op: str) -> None:
    if not state.log_enabled:
        return
    table_name = mapper.local_table.name
    if table_name in LOCAL_TABLES:
        return
    row = _row_dict(mapper, target)
    result = connection.execute(
        insert(_LOG).values(epoch=state.epoch, table_name=table_name, op=op, row=json.dumps(row))
    )
    tracker = current_write.get()
    if tracker is not None:
        tracker.max_seq = max(tracker.max_seq, result.inserted_primary_key[0])


def _after_insert(mapper, connection, target):
    _record(connection, mapper, target, UPSERT)


def _after_update(mapper, connection, target):
    _record(connection, mapper, target, UPSERT)


def _after_delete(mapper, connection, target):
    _record(connection, mapper, target, DELETE)


_installed = False


def install_hooks() -> None:
    """Registra los eventos en todos los modelos (idempotente)."""
    global _installed
    if _installed:
        return
    for mapper in Base.registry.mappers:
        if mapper.local_table.name in LOCAL_TABLES:
            continue
        event.listen(mapper, "after_insert", _after_insert)
        event.listen(mapper, "after_update", _after_update)
        event.listen(mapper, "after_delete", _after_delete)
    _installed = True


# --------------------------------------------------------------------------
# Lectura del log (líder)
# --------------------------------------------------------------------------
def last_seq(conn: Connection) -> int:
    return conn.execute(select(func.max(_LOG.c.seq))).scalar() or 0


def first_seq(conn: Connection) -> int:
    return conn.execute(select(func.min(_LOG.c.seq))).scalar() or 0


def read_entries(conn: Connection, since: int, limit: int = 1000) -> list[dict]:
    rows = conn.execute(select(_LOG).where(_LOG.c.seq > since).order_by(_LOG.c.seq).limit(limit)).mappings()
    return [
        {"seq": r["seq"], "epoch": r["epoch"], "table": r["table_name"], "op": r["op"], "row": r["row"]}
        for r in rows
    ]


def prune(conn: Connection, retention: int) -> int:
    """Poda el log dejando las últimas `retention` entradas (siempre al
    menos la última, que fija la numeración)."""
    top = last_seq(conn)
    cutoff = top - retention
    if cutoff <= 0:
        return 0
    result = conn.execute(_LOG.delete().where(_LOG.c.seq <= cutoff, _LOG.c.seq < top))
    return result.rowcount or 0


# --------------------------------------------------------------------------
# Aplicación (respaldo)
# --------------------------------------------------------------------------
def _decode_row(table, raw: dict) -> dict:
    values = {}
    for column in table.columns:
        if column.name not in raw:
            continue
        value = raw[column.name]
        if value is not None and isinstance(column.type, DateTime) and isinstance(value, str):
            value = datetime.fromisoformat(value)
        values[column.name] = value
    return values


def apply_entries(conn: Connection, entries: list[dict]) -> int:
    """Aplica entradas en orden y las copia al log local. Devuelve el último
    seq aplicado. Llamar dentro de una transacción."""
    tables = {table.name: table for table in replicated_tables()}
    applied = 0
    for entry in entries:
        if entry["op"] != SNAPSHOT_MARKER:
            table = tables.get(entry["table"])
            if table is None:
                raise ValueError(f"Tabla desconocida en el log: {entry['table']!r}")
            values = _decode_row(table, json.loads(entry["row"]))
            if entry["op"] == UPSERT:
                conn.execute(table.insert().prefix_with("OR REPLACE").values(**values))
            elif entry["op"] == DELETE:
                condition = [column == values[column.name] for column in table.primary_key.columns]
                conn.execute(table.delete().where(*condition))
            else:
                raise ValueError(f"Operación desconocida en el log: {entry['op']!r}")
        conn.execute(
            insert(_LOG).values(
                seq=entry["seq"], epoch=entry["epoch"], table_name=entry["table"], op=entry["op"], row=entry["row"]
            )
        )
        applied = entry["seq"]
    return applied


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------
def export_snapshot(conn: Connection) -> dict:
    """Copia consistente (llamar dentro de una transacción de lectura)."""
    data = {}
    for table in replicated_tables():
        data[table.name] = [
            {key: _serialize(value) for key, value in row.items()} for row in conn.execute(select(table)).mappings()
        ]
    return {"seq": last_seq(conn), "epoch": state.epoch, "tables": data}


def import_snapshot(conn: Connection, snapshot: dict) -> int:
    """Reemplaza todo el contenido replicado por el del snapshot y deja el
    log local con una marca en `seq`, para continuar la numeración."""
    tables = replicated_tables()
    for table in reversed(tables):
        conn.execute(table.delete())
    for table in tables:
        rows = [_decode_row(table, raw) for raw in snapshot["tables"].get(table.name, [])]
        if rows:
            conn.execute(table.insert(), rows)
    conn.execute(_LOG.delete())
    seq = int(snapshot["seq"])
    if seq > 0:
        conn.execute(
            insert(_LOG).values(seq=seq, epoch=int(snapshot["epoch"]), table_name="", op=SNAPSHOT_MARKER, row="{}")
        )
    return seq


def local_last_seq(engine: Engine) -> int:
    with engine.connect() as conn:
        return last_seq(conn)
