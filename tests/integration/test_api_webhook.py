from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from app.api.routers.webhooks import router as webhooks_router
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
    app.include_router(webhooks_router)

    try:
        yield app, deps
    finally:
        # Flush any in-flight processor tasks so asyncio doesn't warn about
        # "Task was destroyed but it is pending".
        pending = [t for t in deps.tracked_tasks if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await provider.aclose()
        await api.close()
        await sched.close()
        await web.close()


async def _count_inbox(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        value = await conn.fetchval("SELECT COUNT(*) FROM webhook_inbox")
    return int(value or 0)


async def _fetch_inbox_processed_at(pool: asyncpg.Pool, provider_event_id: str) -> Any:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT processed_at FROM webhook_inbox WHERE provider_event_id = $1",
            provider_event_id,
        )


async def _seed_call_for_provider_id(
    pool: asyncpg.Pool,
    provider_call_id: str,
) -> None:
    # The processor needs a matching `calls.provider_call_id` row; otherwise
    # the event is persisted but logged as WEBHOOK_IGNORED_STALE.
    async with pool.acquire() as conn:
        cid = await CampaignRepo.create(
            conn,
            name="wh",
            timezone="UTC",
            schedule=ALWAYS_ON_SCHEDULE,
            max_concurrent=3,
            retry_config={"max_attempts": 3, "backoff_base_seconds": 1},
        )
        call_ids = await CallRepo.create_batch(
            conn,
            campaign_id=cid,
            phones=["+14155550999"],
            retries_remaining=2,
        )
        await conn.execute(
            """
            UPDATE calls
            SET status = 'DIALING', attempt_epoch = 1, provider_call_id = $2
            WHERE id = $1
            """,
            call_ids[0],
            provider_call_id,
        )


class TestWebhookIngest:
    async def test_valid_payload_inserts_and_acks(
        self,
        app_and_deps: tuple[FastAPI, Deps],
    ) -> None:
        app, deps = app_and_deps
        payload = {
            "provider_event_id": "e-1",
            "provider_call_id": "mock-abc",
            "status": "COMPLETED",
        }
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post("/webhooks/provider", json=payload)
        assert resp.status_code == 200
        assert resp.json() == {"received": True}
        assert await _count_inbox(deps.pools.api) == 1

    async def test_duplicate_event_id_is_idempotent(
        self,
        app_and_deps: tuple[FastAPI, Deps],
    ) -> None:
        app, deps = app_and_deps
        payload = {
            "provider_event_id": "e-dup",
            "provider_call_id": "mock-dup",
            "status": "COMPLETED",
        }
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            r1 = await client.post("/webhooks/provider", json=payload)
            r2 = await client.post("/webhooks/provider", json=payload)
        assert r1.status_code == 200
        assert r2.status_code == 200
        # ON CONFLICT (provider, provider_event_id) preserves a single row.
        async with deps.pools.api.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM webhook_inbox WHERE provider_event_id = $1",
                "e-dup",
            )
        assert count == 1

    async def test_bad_signature_returns_401(
        self,
        app_and_deps: tuple[FastAPI, Deps],
    ) -> None:
        app, deps = app_and_deps

        def _deny(_headers: dict[str, str], _raw: bytes) -> bool:
            return False

        # Swap the verifier and assert no row is written. The ingest must
        # 401 BEFORE any DB side-effect.
        before = await _count_inbox(deps.pools.api)
        original = deps.verify_signature_fn
        deps.verify_signature_fn = _deny
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                resp = await client.post(
                    "/webhooks/provider",
                    json={
                        "provider_event_id": "e-bad-sig",
                        "provider_call_id": "mock-bad",
                        "status": "COMPLETED",
                    },
                )
        finally:
            deps.verify_signature_fn = original
        assert resp.status_code == 401
        after = await _count_inbox(deps.pools.api)
        assert after == before

    async def test_invalid_json_returns_400(
        self,
        app_and_deps: tuple[FastAPI, Deps],
    ) -> None:
        app, _deps = app_and_deps
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                "/webhooks/provider",
                content=b"not-json",
                headers={"content-type": "application/json"},
            )
        assert resp.status_code == 400
        assert "invalid json" in resp.json()["detail"]

    async def test_missing_provider_event_id_returns_400(
        self,
        app_and_deps: tuple[FastAPI, Deps],
    ) -> None:
        app, _deps = app_and_deps
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                "/webhooks/provider",
                json={"provider_call_id": "mock-no-id", "status": "COMPLETED"},
            )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "missing provider_event_id"

    async def test_processor_task_runs_and_marks_processed(
        self,
        app_and_deps: tuple[FastAPI, Deps],
    ) -> None:
        app, deps = app_and_deps
        # Seed a matching calls row so the processor actually applies the
        # transition instead of logging WEBHOOK_IGNORED_STALE.
        provider_call_id = "mock-proc-1"
        await _seed_call_for_provider_id(deps.pools.api, provider_call_id)

        payload = {
            "provider_event_id": "e-proc-1",
            "provider_call_id": provider_call_id,
            "status": "COMPLETED",
        }
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post("/webhooks/provider", json=payload)
        assert resp.status_code == 200

        # Poll for processed_at up to 1s — the processor task is spawned after
        # commit and runs asynchronously relative to the 200 response.
        deadline = asyncio.get_running_loop().time() + 1.0
        processed_at = None
        while asyncio.get_running_loop().time() < deadline:
            processed_at = await _fetch_inbox_processed_at(deps.pools.api, "e-proc-1")
            if processed_at is not None:
                break
            await asyncio.sleep(0.02)
        assert processed_at is not None, "inbox row was not processed within 1s"
