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

from app.api.routers.audit import router as audit_router
from app.audit.emitter import emit_audit
from app.audit.events import AuditEvent
from app.config import Settings
from app.deps import Deps
from app.persistence.pools import Pools
from app.provider.mock import MockProvider, parse_event, verify_signature
from app.scheduler.wake import SchedulerWake

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"


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
    app.include_router(audit_router)

    try:
        yield app, deps
    finally:
        await provider.aclose()
        await api.close()
        await sched.close()
        await web.close()


async def _seed_events(
    pool: asyncpg.Pool,
    *,
    count: int,
    event_type: str = "DISPATCH",
    campaign_id: UUID | None = None,
    reason_prefix: str = "r",
) -> None:
    async with pool.acquire() as conn:
        for i in range(count):
            await emit_audit(
                conn,
                AuditEvent(
                    event_type=event_type,  # type: ignore[arg-type]
                    reason=f"{reason_prefix}-{i:04d}",
                    campaign_id=campaign_id,
                ),
            )


class TestAuditPagination:
    async def test_paginate_three_pages(
        self,
        app_and_deps: tuple[FastAPI, Deps],
    ) -> None:
        app, deps = app_and_deps
        await _seed_events(deps.pools.api, count=250)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            # Page 1
            r1 = await client.get("/audit", params={"limit": 100})
            assert r1.status_code == 200
            body1 = r1.json()
            assert len(body1["events"]) == 100
            assert body1["next_cursor"] is not None

            # Page 2
            r2 = await client.get(
                "/audit",
                params={"limit": 100, "cursor": body1["next_cursor"]},
            )
            assert r2.status_code == 200
            body2 = r2.json()
            assert len(body2["events"]) == 100
            assert body2["next_cursor"] is not None

            # Page 3 — 50 remaining, cursor None.
            r3 = await client.get(
                "/audit",
                params={"limit": 100, "cursor": body2["next_cursor"]},
            )
            assert r3.status_code == 200
            body3 = r3.json()
            assert len(body3["events"]) == 50
            assert body3["next_cursor"] is None

            # No id collisions across pages — each row surfaces exactly once.
            ids_all = {e["id"] for e in body1["events"] + body2["events"] + body3["events"]}
            assert len(ids_all) == 250


class TestAuditFilters:
    async def test_filter_single_event_type(
        self,
        app_and_deps: tuple[FastAPI, Deps],
    ) -> None:
        app, deps = app_and_deps
        cid = uuid4()
        await _seed_events(deps.pools.api, count=5, event_type="DISPATCH", campaign_id=cid)
        await _seed_events(deps.pools.api, count=3, event_type="TRANSITION", campaign_id=cid)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/audit",
                params={"event_type": "DISPATCH", "campaign_id": str(cid)},
            )
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 5
        assert all(e["event_type"] == "DISPATCH" for e in events)

    async def test_filter_comma_separated_event_type(
        self,
        app_and_deps: tuple[FastAPI, Deps],
    ) -> None:
        app, deps = app_and_deps
        cid = uuid4()
        await _seed_events(deps.pools.api, count=4, event_type="DISPATCH", campaign_id=cid)
        await _seed_events(deps.pools.api, count=2, event_type="TRANSITION", campaign_id=cid)
        await _seed_events(deps.pools.api, count=3, event_type="RETRY_DUE", campaign_id=cid)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/audit",
                params={
                    "event_type": "DISPATCH,TRANSITION",
                    "campaign_id": str(cid),
                },
            )
        assert resp.status_code == 200
        events = resp.json()["events"]
        # 4 DISPATCH + 2 TRANSITION; RETRY_DUE excluded.
        assert len(events) == 6
        types = {e["event_type"] for e in events}
        assert types == {"DISPATCH", "TRANSITION"}

    async def test_and_composition_campaign_and_event_type(
        self,
        app_and_deps: tuple[FastAPI, Deps],
    ) -> None:
        app, deps = app_and_deps
        cid_a = uuid4()
        cid_b = uuid4()
        # A: 3 TRANSITION + 2 DISPATCH. B: 4 TRANSITION.
        await _seed_events(deps.pools.api, count=3, event_type="TRANSITION", campaign_id=cid_a)
        await _seed_events(deps.pools.api, count=2, event_type="DISPATCH", campaign_id=cid_a)
        await _seed_events(deps.pools.api, count=4, event_type="TRANSITION", campaign_id=cid_b)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/audit",
                params={"campaign_id": str(cid_a), "event_type": "TRANSITION"},
            )
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 3
        assert all(e["event_type"] == "TRANSITION" for e in events)
        assert all(e["campaign_id"] == str(cid_a) for e in events)

    async def test_limit_over_max_returns_422(
        self,
        app_and_deps: tuple[FastAPI, Deps],
    ) -> None:
        app, _deps = app_and_deps
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/audit", params={"limit": 501})
        # Pydantic / FastAPI Query(le=500) → 422 validation error.
        assert resp.status_code == 422
