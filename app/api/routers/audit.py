from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.schemas.audit import AuditEventResponse, AuditListResponse
from app.audit.reader import query_audit
from app.deps import Deps

router = APIRouter(tags=["audit"])


def get_deps(request: Request) -> Deps:
    deps: Deps = request.app.state.deps
    return deps


# Using the Annotated[..., Depends/Query(...)] form keeps FastAPI's DI idiom
# while avoiding the B008 "function call in default argument" lint.
DepsDep = Annotated[Deps, Depends(get_deps)]


@router.get("/audit", response_model=AuditListResponse)
async def list_audit(
    deps: DepsDep,
    campaign_id: Annotated[UUID | None, Query()] = None,
    event_type: Annotated[
        str | None,
        Query(description="Single event type or comma-separated list (OR-composed)."),
    ] = None,
    from_ts: Annotated[datetime | None, Query()] = None,
    to_ts: Annotated[datetime | None, Query()] = None,
    reason_contains: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> AuditListResponse:
    # The query param accepts a single name or a comma-separated list. The
    # reader's public shape uses `str` for a single value and a sequence for
    # a multi-value IN filter — compose that here so the demo seed URLs
    # (e.g. event_type=RETRY_DUE,DISPATCH) work without extra client work.
    event_filter: str | list[str] | None = None
    if event_type:
        items = [s.strip() for s in event_type.split(",") if s.strip()]
        if len(items) > 1:
            event_filter = items
        elif len(items) == 1:
            event_filter = items[0]

    rows, next_cursor = await query_audit(
        deps.pools.api,
        campaign_id=campaign_id,
        event_type=event_filter,
        from_ts=from_ts,
        to_ts=to_ts,
        reason_contains=reason_contains,
        cursor=cursor,
        limit=limit,
    )
    events = [
        AuditEventResponse(
            id=r.id,
            ts=r.ts,
            event_type=r.event_type,
            campaign_id=r.campaign_id,
            call_id=r.call_id,
            reason=r.reason,
            state_before=r.state_before,
            state_after=r.state_after,
            extra=r.extra,
        )
        for r in rows
    ]
    return AuditListResponse(events=events, next_cursor=next_cursor)
