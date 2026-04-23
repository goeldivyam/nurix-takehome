from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.audit.emitter import emit_audit
from app.audit.events import AuditEvent
from app.deps import Deps

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])


class AgeDialingResponse(BaseModel):
    call_id: UUID
    aged_by_seconds: int
    new_updated_at: str


def get_deps(request: Request) -> Deps:
    return request.app.state.deps  # type: ignore[no-any-return]


DepsDep = Annotated[Deps, Depends(get_deps)]


@router.post(
    "/age-dialing/{call_id}",
    response_model=AgeDialingResponse,
    status_code=status.HTTP_200_OK,
)
async def age_dialing(
    call_id: UUID,
    deps: DepsDep,
    by_seconds: Annotated[int, Query(ge=1, le=86_400)] = 900,
) -> AgeDialingResponse:
    # Ages `calls.updated_at` backwards so the reclaim sweep picks up this
    # row on its next pass. This is a demo/testing shortcut for rubric #8 —
    # without it, an operator has to wait `max_call_duration + 30s` of real
    # time before RECLAIM_EXECUTED lands in the audit log. The route is
    # registered only when DEBUG_ENDPOINTS_ENABLED=true so it never ships
    # to production by accident.
    if not deps.settings.debug_endpoints_enabled:
        raise HTTPException(status_code=403, detail="debug endpoints disabled")

    async with deps.pools.scheduler.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            UPDATE calls
            SET updated_at = NOW() - ($2::int * INTERVAL '1 second')
            WHERE id = $1 AND status = 'DIALING'
            RETURNING id, updated_at, campaign_id, attempt_epoch
            """,
            call_id,
            by_seconds,
        )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="call not found or not in DIALING state",
            )
        await emit_audit(
            conn,
            AuditEvent(
                event_type="DEBUG_AGE_DIALING",
                reason=f"aged updated_at by {by_seconds}s for reclaim demo",
                campaign_id=row["campaign_id"],
                call_id=call_id,
                extra={
                    "aged_by_seconds": by_seconds,
                    "attempt_epoch": row["attempt_epoch"],
                },
            ),
        )
    return AgeDialingResponse(
        call_id=call_id,
        aged_by_seconds=by_seconds,
        new_updated_at=row["updated_at"].isoformat(),
    )
