"""
Estado de HA de este ControlNode (en memoria, salvo la época, que se
persiste en `ha_meta`).

Roles:
- `leader`: atiende a clientes y DataNodes, genera el log de cambios.
- `standby`: sigue al líder aplicando su log; rechaza el resto de
  peticiones con 503 `not_leader`. Toma el control si el líder cae.

La época (`epoch`) crece cada vez que un nodo se convierte en líder. Sirve
de *fencing*: un líder que descubre otro con época mayor se degrada, y los
DataNodes ignoran órdenes de líderes con época menor a la que ya vieron.
"""
from __future__ import annotations

import contextvars
import threading
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import HAMeta

LEADER = "leader"
STANDBY = "standby"

# Un respaldo que no pide el log en este tiempo deja de contar para la
# espera semi-síncrona (no se bloquea al líder por un respaldo caído).
FOLLOWER_HEALTHY_SECONDS = 5.0
# Los informes de testigo de un DataNode valen este tiempo.
WITNESS_VALID_SECONDS = 30.0


@dataclass
class WriteTracker:
    """Por petición: la última entrada del log que generó (para que el
    middleware espere a que el respaldo la tenga antes de responder)."""

    max_seq: int = 0


current_write: contextvars.ContextVar[WriteTracker | None] = contextvars.ContextVar("ha_write", default=None)


@dataclass
class HAState:
    enabled: bool = False
    role: str = LEADER
    epoch: int = 1
    leader_id: str | None = None
    leader_url: str | None = None
    applied_seq: int = 0
    # follower_id -> (seq confirmado, instante monotónico del último pedido)
    follower_acks: dict[str, tuple[int, float]] = field(default_factory=dict)
    # datanode_id -> ({controlnode_id: segundos sin contacto}, instante)
    witness: dict[str, tuple[dict[str, float], float]] = field(default_factory=dict)
    last_leader_contact: float = field(default_factory=time.monotonic)
    need_snapshot: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def is_leader(self) -> bool:
        return self.role == LEADER

    @property
    def log_enabled(self) -> bool:
        return self.enabled and self.role == LEADER

    # -- respaldos -----------------------------------------------------------
    def record_follower(self, follower_id: str, seq: int) -> None:
        with self.lock:
            self.follower_acks[follower_id] = (seq, time.monotonic())

    def healthy_followers(self) -> dict[str, int]:
        now = time.monotonic()
        with self.lock:
            return {
                fid: seq for fid, (seq, seen) in self.follower_acks.items() if now - seen <= FOLLOWER_HEALTHY_SECONDS
            }

    # -- testigos ------------------------------------------------------------
    def record_witness(self, datanode_id: str, view: dict[str, float]) -> None:
        with self.lock:
            self.witness[datanode_id] = (dict(view), time.monotonic())

    def witnesses_seeing(self, controlnode_id: str, within: float) -> tuple[int, int]:
        """(DataNodes con informe reciente, cuántos de ellos ven a
        `controlnode_id` vivo: contacto OK hace menos de `within` s)."""
        now = time.monotonic()
        reporters = seeing = 0
        with self.lock:
            for view, received in self.witness.values():
                if now - received > WITNESS_VALID_SECONDS:
                    continue
                reporters += 1
                ago = view.get(controlnode_id)
                if ago is not None and ago < within:
                    seeing += 1
        return reporters, seeing

    def reset(self) -> None:
        """Vuelve al estado por defecto (ControlNode único). Para tests."""
        self.enabled = False
        self.role = LEADER
        self.epoch = 1
        self.leader_id = settings.controlnode_id
        self.leader_url = None
        self.applied_seq = 0
        self.follower_acks.clear()
        self.witness.clear()
        self.need_snapshot = False
        self.last_leader_contact = time.monotonic()


state = HAState(leader_id=settings.controlnode_id)


# --- época persistente ---------------------------------------------------------
def load_epoch(db: Session) -> int:
    row = db.get(HAMeta, "epoch")
    return int(row.value) if row else 0


def save_epoch(epoch: int) -> None:
    db = SessionLocal()
    try:
        row = db.get(HAMeta, "epoch")
        if row is None:
            db.add(HAMeta(key="epoch", value=str(epoch)))
        else:
            row.value = str(epoch)
        db.commit()
    finally:
        db.close()


def status_dict() -> dict:
    return {
        "controlnode_id": settings.controlnode_id,
        "role": state.role,
        "epoch": state.epoch,
        "leader_id": state.leader_id,
        "applied_seq": state.applied_seq,
        "public_url": settings.public_url or None,
        "ha_enabled": state.enabled,
    }
