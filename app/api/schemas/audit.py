from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class AuditEventResponse(BaseModel):
    id: int
    ts: datetime
    event_type: str
    campaign_id: UUID | None
    call_id: UUID | None
    reason: str
    state_before: str | None
    state_after: str | None
    extra: dict[str, Any]


class AuditListResponse(BaseModel):
    events: list[AuditEventResponse]
    next_cursor: str | None
