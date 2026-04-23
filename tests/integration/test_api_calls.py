from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from app.api.routers.calls import router as calls_router
from app.config import Settings
from app.deps import Deps
from app.persistence.pools import Pools
from app.persistence.repositories import CallRepo, CampaignRepo
from app.provider.mock import MockProvider, parse_event, verify_signature
from app.scheduler.wake import SchedulerWake

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"

ALWAYS_ON_SCHEDULE: dict[str, Any] = {
    "mon": [{"start": "00:00", "end": "23:59"}],
    "tue": [{"start": "00:00", "end": "23:59"}],
    "wed": [{"start": "00:00", "end": "23:59"}],
    "thu": [{"start": "00:00", "end": "23:59"}],
    "fri": [{"start": "00:00", "end": "23:59"}],
    "sat": [{"start": "00:00", "end": "23:59"}],
    "sun": [{"start": "00:00", "end": "23:59"}],
}

DEFAULT_RETRY_CONFIG: dict[str, Any] = {"max_attempts": 3, "backoff_base_seconds": 1}


@pytest.fixture(scope="module")
def pg_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as container:
        yield container


@pytest.fixture
async def app_and_deps(
    pg_container: PostgresContainer,
) -> AsyncIterator[tuple[FastAPI, Deps]]:
    raw_url = pg_container.get_connection_url()
    dsn = raw_url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+psycopg://", "postgresql://"
    )
    api = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    sched = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    web = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    assert api is not None
    assert sched is not None
    assert web is not None

    schema_sql = SCHEMA_PATH.read_text()
    async with api.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.execute(schema_sql)

    settings = Settings(database_url=dsn)
    pools = Pools(api=api, scheduler=sched, webhook=web)

    async def sink(payload: dict[str, Any]) -> None:
        del payload

    provider = MockProvider(settings, event_sink=sink)
    deps = Deps(
        settings=settings,
        pools=pools,
        provider=provider,
        wake=SchedulerWake(),
        tracked_tasks=set(),
        parse_event_fn=parse_event,
        verify_signature_fn=verify_signature,
    )

    app = FastAPI()
    app.state.deps = deps
    app.include_router(calls_router)

    try:
        yield app, deps
    finally:
        await provider.aclose()
        await api.close()
        await sched.close()
        await web.close()


async def _seed_campaign(pool: asyncpg.Pool) -> UUID:
    async with pool.acquire() as conn:
        return await CampaignRepo.create(
            conn,
            name="test",
            timezone="UTC",
            schedule=ALWAYS_ON_SCHEDULE,
            max_concurrent=3,
            retry_config=DEFAULT_RETRY_CONFIG,
        )


async def _seed_call(
    pool: asyncpg.Pool,
    campaign_id: UUID,
    phone: str,
    status: str,
) -> UUID:
    async with pool.acquire() as conn:
        ids = await CallRepo.create_batch(
            conn,
            campaign_id=campaign_id,
            phones=[phone],
            retries_remaining=2,
        )
        if status != "QUEUED":
            # IN_PROGRESS / DIALING require the phone-partial-unique slot; our
            # set of statuses is curated so each call uses a distinct phone.
            await conn.execute(
                "UPDATE calls SET status = $2 WHERE id = $1",
                ids[0],
                status,
            )
        return ids[0]


_INTERNAL_TO_EXTERNAL: dict[str, str] = {
    "QUEUED": "in_progress",
    "DIALING": "in_progress",
    "IN_PROGRESS": "in_progress",
    "RETRY_PENDING": "in_progress",
    "COMPLETED": "completed",
    "FAILED": "failed",
    "NO_ANSWER": "failed",
    "BUSY": "failed",
}


class TestCallsRoute:
    @pytest.mark.parametrize(
        "internal_status",
        [
            "QUEUED",
            "DIALING",
            "IN_PROGRESS",
            "RETRY_PENDING",
            "COMPLETED",
            "FAILED",
            "NO_ANSWER",
            "BUSY",
        ],
    )
    async def test_status_mapping_per_internal_state(
        self,
        app_and_deps: tuple[FastAPI, Deps],
        internal_status: str,
    ) -> None:
        app, deps = app_and_deps
        cid = await _seed_campaign(deps.pools.api)
        # Distinct phone per status so the partial unique index never collides
        # across the parametrized cases that share the module-scoped DB.
        phone = f"+1415555{hash(internal_status) & 0xFFFF:04d}"
        call_id = await _seed_call(deps.pools.api, cid, phone, internal_status)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(f"/calls/{call_id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == str(call_id)
        assert body["campaign_id"] == str(cid)
        assert body["phone"] == phone
        assert body["status"] == _INTERNAL_TO_EXTERNAL[internal_status]

    async def test_unknown_id_returns_404(
        self,
        app_and_deps: tuple[FastAPI, Deps],
    ) -> None:
        app, _deps = app_and_deps
        missing = uuid4()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(f"/calls/{missing}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "call not found"
