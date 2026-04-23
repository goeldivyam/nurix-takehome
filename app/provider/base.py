from __future__ import annotations

from typing import Protocol

from app.provider.types import CallHandle
from app.state.types import CallStatus


class TelephonyProvider(Protocol):
    # Call-placement port only. parse_event / verify_signature live as
    # module-level functions on each adapter and are wired onto `deps` by the
    # FastAPI lifespan; promoting them onto this Protocol is deferred until a
    # second adapter (Twilio / Retell / Vapi) exists to ground the shape.

    async def place_call(self, idempotency_key: str, phone: str) -> CallHandle: ...

    async def get_status(self, call_id: str) -> CallStatus: ...

    async def aclose(self) -> None: ...
