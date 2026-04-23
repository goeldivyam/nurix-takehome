from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.deps import Deps
from app.persistence.repositories import CallRepo

router = APIRouter(tags=["calls"])

# External status mapping per assignment: the caller only cares about
# in_progress | completed | failed. The internal state machine is finer
# (QUEUED, DIALING, IN_PROGRESS, RETRY_PENDING, COMPLETED, FAILED,
# NO_ANSWER, BUSY) but those are implementation detail — retries are still
# "in flight" from the caller's POV, and NO_ANSWER / BUSY fold into failed.
_EXTERNAL_STATUS_MAP: dict[str, str] = {
    "QUEUED": "in_progress",
    "DIALING": "in_progress",
    "IN_PROGRESS": "in_progress",
    "RETRY_PENDING": "in_progress",
    "COMPLETED": "completed",
    "FAILED": "failed",
    "NO_ANSWER": "failed",
    "BUSY": "failed",
}


class CallResponse(BaseModel):
    id: UUID
    campaign_id: UUID
    phone: str
    status: str


def get_deps(request: Request) -> Deps:
    deps: Deps = request.app.state.deps
    return deps


# FastAPI's Depends/Query-in-default idiom is the framework's canonical
# signature shape; B008 (do-not-call-in-defaults) is suppressed on each
# parameter line rather than re-architecting away from the idiom.
@router.get("/calls/{call_id}", response_model=CallResponse)
async def get_call(
    call_id: UUID,
    deps: Deps = Depends(get_deps),  # noqa: B008
) -> CallResponse:
    async with deps.pools.api.acquire() as conn:
        row = await CallRepo.get(conn, call_id)
    if row is None:
        raise HTTPException(status_code=404, detail="call not found")
    return CallResponse(
        id=row.id,
        campaign_id=row.campaign_id,
        phone=row.phone,
        status=_EXTERNAL_STATUS_MAP[row.status],
    )
