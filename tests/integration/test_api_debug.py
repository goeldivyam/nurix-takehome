from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from app.api.routers.debug import router as debug_router
from app.config import Settings
from app.deps import Deps
from app.persistence.pools import Pools
from app.provider.mock import MockProvider, parse_event, verify_signature
from app.scheduler.wake import SchedulerWake


@pytest.fixture(scope="module")
def _pg() -> AsyncIterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as c:
        yield c


async def _make_client(
    pg: PostgresContainer, *, debug_enabled: bool
) -> tuple[AsyncClient, Deps, Pools, MockProvider]:
    dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    api = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    sched = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    web = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    assert api is not None
    assert sched is not None
    assert web is not None
    async with api.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.execute(Path("schema.sql").read_text())

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=dsn,
        debug_endpoints_enabled=debug_enabled,
    )
    pools = Pools(api=api, scheduler=sched, webhook=web)
    wake = SchedulerWake()

    async def sink(payload: dict[str, object]) -> None:
        pass

    provider = MockProvider(settings, event_sink=sink)
    deps = Deps(
        settings=settings,
        pools=pools,
        provider=provider,
        wake=wake,
        tracked_tasks=set(),
        parse_event_fn=parse_event,
        verify_signature_fn=verify_signature,
    )
    app = FastAPI()
    app.state.deps = deps
    app.include_router(debug_router)
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    return client, deps, pools, provider


async def _seed_dialing_call(pools: Pools) -> tuple[UUID, UUID]:
    async with pools.scheduler.acquire() as conn:
        campaign_id = await conn.fetchval(
            """
            INSERT INTO campaigns
              (name, status, timezone, schedule, max_concurrent, retry_config)
            VALUES ('debug-test', 'ACTIVE', 'UTC', '{}'::jsonb, 3, '{}'::jsonb)
            RETURNING id
            """
        )
        call_id = await conn.fetchval(
            """
            INSERT INTO calls
              (campaign_id, phone, status, attempt_epoch, retries_remaining,
               provider_call_id, updated_at)
            VALUES ($1, '+14155550001', 'DIALING', 1, 2, 'mock-pcid', NOW())
            RETURNING id
            """,
            campaign_id,
        )
    return campaign_id, call_id


async def test_age_dialing_moves_updated_at_backwards(
    _pg: PostgresContainer,  # noqa: PT019 -- DSN extraction requires the fixture value
) -> None:
    client, _deps, pools, provider = await _make_client(_pg, debug_enabled=True)
    try:
        _, call_id = await _seed_dialing_call(pools)
        before = datetime.now(tz=UTC)

        resp = await client.post(f"/debug/age-dialing/{call_id}?by_seconds=900")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["call_id"] == str(call_id)
        assert body["aged_by_seconds"] == 900

        async with pools.api.acquire() as conn:
            row = await conn.fetchrow("SELECT updated_at, status FROM calls WHERE id = $1", call_id)
            assert row is not None
            assert row["status"] == "DIALING"
            # updated_at is now >= 900s in the past relative to the moment the
            # call landed — allow a small clock-skew margin.
            delta = (before - row["updated_at"]).total_seconds()
            assert delta >= 890

            audit = await conn.fetchrow(
                """
                SELECT event_type, reason, extra
                FROM scheduler_audit
                WHERE call_id = $1 AND event_type = 'DEBUG_AGE_DIALING'
                """,
                call_id,
            )
            assert audit is not None
            assert audit["event_type"] == "DEBUG_AGE_DIALING"
    finally:
        await provider.aclose()
        await client.aclose()
        await pools.api.close()
        await pools.scheduler.close()
        await pools.webhook.close()


async def test_age_dialing_403_when_flag_disabled(
    _pg: PostgresContainer,  # noqa: PT019 -- DSN extraction requires the fixture value
) -> None:
    client, _deps, pools, provider = await _make_client(_pg, debug_enabled=False)
    try:
        _, call_id = await _seed_dialing_call(pools)
        resp = await client.post(f"/debug/age-dialing/{call_id}")
        assert resp.status_code == 403, resp.text
        assert "disabled" in resp.json()["detail"]
    finally:
        await provider.aclose()
        await client.aclose()
        await pools.api.close()
        await pools.scheduler.close()
        await pools.webhook.close()


async def test_age_dialing_404_on_unknown_or_non_dialing(
    _pg: PostgresContainer,  # noqa: PT019 -- DSN extraction requires the fixture value
) -> None:
    client, _deps, pools, provider = await _make_client(_pg, debug_enabled=True)
    try:
        # Unknown call id.
        unknown = uuid4()
        resp = await client.post(f"/debug/age-dialing/{unknown}")
        assert resp.status_code == 404

        # Call that exists but is QUEUED (not DIALING).
        async with pools.scheduler.acquire() as conn:
            campaign_id = await conn.fetchval(
                """
                INSERT INTO campaigns
                  (name, status, timezone, schedule, max_concurrent, retry_config)
                VALUES ('debug-404', 'ACTIVE', 'UTC', '{}'::jsonb, 3, '{}'::jsonb)
                RETURNING id
                """
            )
            queued_id = await conn.fetchval(
                """
                INSERT INTO calls
                  (campaign_id, phone, status, attempt_epoch, retries_remaining)
                VALUES ($1, '+14155559999', 'QUEUED', 0, 2)
                RETURNING id
                """,
                campaign_id,
            )
        resp = await client.post(f"/debug/age-dialing/{queued_id}")
        assert resp.status_code == 404
    finally:
        await provider.aclose()
        await client.aclose()
        await pools.api.close()
        await pools.scheduler.close()
        await pools.webhook.close()
