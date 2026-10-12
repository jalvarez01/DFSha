"""
Middleware ASGI de HA.

1. En un respaldo, toda petición que no sea de HA, salud o heartbeat se
   rechaza con 503 `not_leader` (más el header X-DFSha-Leader con el id del
   líder): los clientes y DataNodes prueban el siguiente ControlNode de su
   lista.

2. En el líder, si la petición escribió metadatos (generó entradas en el
   log), la respuesta espera a que todos los respaldos SANOS confirmen esas
   entradas, como mucho `ha_sync_timeout` segundos. Así, cuando un cliente
   recibe la respuesta de una escritura, esta ya está en el respaldo y un
   failover no la pierde (replicación semi-síncrona). Si el respaldo está
   caído o no confirma a tiempo, el líder sigue respondiendo (prima la
   disponibilidad) y el respaldo se pondrá al día después.
"""
from __future__ import annotations

import asyncio
import time

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings
from app.ha.state import WriteTracker, current_write, state

_ALLOWED_ON_STANDBY = ("/health", "/ha/", "/datanodes/heartbeat", "/docs", "/openapi.json", "/redoc")


async def wait_for_followers(seq: int, timeout: float | None = None) -> bool:
    """True si todos los respaldos sanos confirmaron `seq` a tiempo."""
    deadline = time.monotonic() + (settings.ha_sync_timeout if timeout is None else timeout)
    while True:
        healthy = state.healthy_followers()
        if not healthy or all(acked >= seq for acked in healthy.values()):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.01)


class HAMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not state.enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not state.is_leader and not path.startswith(_ALLOWED_ON_STANDBY):
            response = JSONResponse(
                status_code=503,
                content={
                    "detail": {
                        "error": "not_leader",
                        "message": f"{settings.controlnode_id} es un respaldo; habla con el líder",
                        "leader_id": state.leader_id,
                    }
                },
                headers={"X-DFSha-Leader": state.leader_id or ""},
            )
            await response(scope, receive, send)
            return

        tracker = WriteTracker()
        token = current_write.set(tracker)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and tracker.max_seq and state.is_leader:
                await wait_for_followers(tracker.max_seq)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            current_write.reset(token)
