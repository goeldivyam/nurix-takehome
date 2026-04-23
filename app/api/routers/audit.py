from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.schemas.audit import AuditEventResponse, AuditListResponse
from app.audit.reader import query_audit
from app.deps import Deps

router = APIRouter(tags=["audit"])


def get_deps(request: Request) -> Deps:
    deps: Deps = request.app.state.deps
    return deps


# FastAPI's Depends/Query-in-default idiom is the framework's canonical
# signature shape; B008 (do-not-call-in-defaults) is suppressed per line
# rather than re-architecting away from the idiom.
@router.get("/audit", response_model=AuditListResponse)
async def list_audit(
    campaign_id: UUID | None = Query(default=None),  # noqa: B008
    event_type: str | None = Query(
        default=None,
        description="Single event type or comma-separated list (OR-composed).",
    ),
    from_ts: datetime | None = Query(default=None),  # noqa: B008
    to_ts: datetime | None = Query(default=None),  # noqa: B008
    reason_contains: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    deps: Deps = Depends(get_deps),  # noqa: B008
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
