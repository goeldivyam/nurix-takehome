from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from app.config import Settings


@dataclass(frozen=True, slots=True)
class Pools:
    # Three role-segregated asyncpg pools. A webhook burst must not starve the
    # API or the scheduler tick loop; `/audit` reads go to `api`, never to
    # `scheduler` (rubric #7 — observability must not steal tick capacity).
    api: asyncpg.Pool
    scheduler: asyncpg.Pool
    webhook: asyncpg.Pool


async def create_pools(settings: Settings) -> Pools:
    api = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.api_pool_min,
        max_size=settings.api_pool_max,
    )
    scheduler = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.scheduler_pool_min,
        max_size=settings.scheduler_pool_max,
    )
    webhook = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.webhook_pool_min,
        max_size=settings.webhook_pool_max,
    )
    if api is None or scheduler is None or webhook is None:
        raise RuntimeError("asyncpg.create_pool returned None")
    return Pools(api=api, scheduler=scheduler, webhook=webhook)


async def close_pools(pools: Pools) -> None:
    await pools.api.close()
    await pools.scheduler.close()
    await pools.webhook.close()
