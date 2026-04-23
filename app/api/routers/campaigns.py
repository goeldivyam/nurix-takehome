from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.schemas.campaigns import (
    CampaignCreate,
    CampaignListResponse,
    CampaignResponse,
    CampaignStatsResponse,
    RetryConfig,
    TimeWindow,
)
from app.deps import Deps
from app.persistence.repositories import CallRepo, CampaignRepo, CampaignRow

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

_WEEKDAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def get_deps(request: Request) -> Deps:
    # FastAPI dependency — reads the Deps container stashed by the lifespan
    # setup onto `app.state`. Routes never build their own Deps; the lifespan
    # owns pool / provider / wake lifetimes.
    deps: Deps = request.app.state.deps
    return deps


# Using the Annotated[..., Depends(...)] form avoids the B008 "function call in
# default argument" lint while remaining the idiomatic FastAPI DI pattern.
DepsDep = Annotated[Deps, Depends(get_deps)]


def _row_to_response(row: CampaignRow) -> CampaignResponse:
    # CampaignRow normalizes JSONB through `_loads_json` in the repo, so
    # `row.schedule` and `row.retry_config` are already dicts. Re-hydrate them
    # into the typed API response models so the response stays schema-checked.
    sched_raw: dict[str, Any] = row.schedule or {}
    schedule: dict[str, list[TimeWindow]] = {
        key: [TimeWindow(**w) for w in (sched_raw.get(key) or [])] for key in _WEEKDAY_KEYS
    }
    retry = RetryConfig(**row.retry_config)
    return CampaignResponse(
        id=row.id,
        name=row.name,
        status=row.status,
        timezone=row.timezone,
        schedule=schedule,
        max_concurrent=row.max_concurrent,
        retry_config=retry,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CampaignResponse)
async def create_campaign(
    body: CampaignCreate,
    deps: DepsDep,
) -> CampaignResponse:
    max_concurrent = body.max_concurrent or deps.settings.max_concurrent_default
    max_retries = body.retry_config.max_attempts

    # Exception to the "reads use api_pool" convention: the initial seed insert
    # is a scheduler-owned write surface — the calls created here enter the
    # state machine's state space. Subsequent reads (list / detail / stats)
    # use api_pool as normal.
    async with deps.pools.scheduler.acquire() as conn, conn.transaction():
        schedule_json: dict[str, Any] = {
            key: [w.model_dump(mode="json") for w in windows]
            for key, windows in body.schedule.items()
        }
        campaign_id = await CampaignRepo.create(
            conn,
            name=body.name,
            timezone=body.timezone,
            schedule=schedule_json,
            max_concurrent=max_concurrent,
            retry_config=body.retry_config.model_dump(mode="json"),
        )
        await CallRepo.create_batch(
            conn,
            campaign_id=campaign_id,
            phones=body.phones,
            retries_remaining=max_retries,
        )
        row = await CampaignRepo.get(conn, campaign_id)

    # Wake the scheduler so it picks up this campaign on the next tick instead
    # of waiting for the safety-net timeout to fire.
    deps.wake.notify()

    if row is None:
        # Should be unreachable — the insert committed inside the same txn.
        raise HTTPException(status_code=500, detail="campaign disappeared after insert")
    return _row_to_response(row)


@router.get("", response_model=CampaignListResponse)
async def list_campaigns(
    deps: DepsDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> CampaignListResponse:
    rows, next_cursor = await CampaignRepo.list_page(deps.pools.api, cursor, limit)
    return CampaignListResponse(
        campaigns=[_row_to_response(r) for r in rows],
        next_cursor=next_cursor,
    )


@router.get("/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(
    campaign_id: UUID,
    deps: DepsDep,
) -> CampaignResponse:
    async with deps.pools.api.acquire() as conn:
        row = await CampaignRepo.get(conn, campaign_id)
    if row is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return _row_to_response(row)


@router.get("/{campaign_id}/stats", response_model=CampaignStatsResponse)
async def get_campaign_stats(
    campaign_id: UUID,
    deps: DepsDep,
) -> CampaignStatsResponse:
    async with deps.pools.api.acquire() as conn:
        exists = await CampaignRepo.get(conn, campaign_id)
    if exists is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    stats = await CampaignRepo.stats(deps.pools.api, campaign_id)
    return CampaignStatsResponse(
        total=stats.total,
        completed=stats.completed,
        failed=stats.failed,
        retries_attempted=stats.retries_attempted,
        in_progress=stats.in_progress,
    )
