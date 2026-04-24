from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from app.api.routers.campaigns import router as campaigns_router
from app.config import Settings
from app.deps import Deps
from app.persistence.pools import Pools
from app.provider.mock import MockProvider, parse_event, verify_signature
from app.scheduler.wake import SchedulerWake

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"

DEFAULT_SCHEDULE: dict[str, list[dict[str, str]]] = {
    "mon": [{"start": "00:00", "end": "23:59"}],
    "tue": [{"start": "00:00", "end": "23:59"}],
    "wed": [{"start": "00:00", "end": "23:59"}],
    "thu": [{"start": "00:00", "end": "23:59"}],
    "fri": [{"start": "00:00", "end": "23:59"}],
    "sat": [{"start": "00:00", "end": "23:59"}],
    "sun": [{"start": "00:00", "end": "23:59"}],
}

DEFAULT_RETRY_CONFIG: dict[str, int] = {"max_attempts": 3, "backoff_base_seconds": 30}


# -- fixtures ----------------------------------------------------------------


@pytest.fixture(scope="module")
def pg() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as c:
        yield c


@pytest.fixture
async def deps(pg: PostgresContainer) -> AsyncIterator[Deps]:
    # Three distinct asyncpg pools so the route's `pools.scheduler` vs
    # `pools.api` split is exercised end-to-end, matching the production
    # lifespan wiring — pool segregation is load-bearing (CLAUDE.md,
    # backend-conventions).
    dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    api = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    sched = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    web = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    assert api is not None
    assert sched is not None
    assert web is not None
    async with api.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.execute(SCHEMA_PATH.read_text())

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        max_concurrent_default=7,
        mock_call_duration_seconds=0.05,
    )
    pools = Pools(api=api, scheduler=sched, webhook=web)
    wake = SchedulerWake()

    async def noop_sink(_payload: dict[str, Any]) -> None:
        return None

    provider = MockProvider(settings, noop_sink)
    tracked: set[asyncio.Task[Any]] = set()
    deps_obj = Deps(
        settings=settings,
        pools=pools,
        provider=provider,
        wake=wake,
        tracked_tasks=tracked,
        parse_event_fn=parse_event,
        verify_signature_fn=verify_signature,
    )
    try:
        yield deps_obj
    finally:
        await provider.aclose()
        await api.close()
        await sched.close()
        await web.close()


@pytest.fixture
async def client(deps: Deps) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.state.deps = deps
    app.include_router(campaigns_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _valid_campaign_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "nurix-demo",
        "timezone": "America/Los_Angeles",
        "schedule": DEFAULT_SCHEDULE,
        "retry_config": DEFAULT_RETRY_CONFIG,
        # Both +1 (US) and +91 (India) are normalized to E.164 — Nurix runs
        # campaigns in both regions and the validator requires explicit +cc
        # so no dial plan is guessed.
        "phones": ["+14155551234", "+14155551235", "+919876543210"],
    }
    body.update(overrides)
    return body


# -- tests -------------------------------------------------------------------


class TestPostCampaign:
    async def test_post_valid_campaign_201(self, client: AsyncClient, deps: Deps) -> None:
        body = _valid_campaign_body()
        resp = await client.post("/campaigns", json=body)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["name"] == "nurix-demo"
        # External status vocabulary per assignment ("pending, in-progress,
        # completed, failed"). Internal DB column remains `PENDING`.
        assert data["status"] == "pending"
        assert data["timezone"] == "America/Los_Angeles"
        assert data["max_concurrent"] == deps.settings.max_concurrent_default
        assert data["retry_config"] == DEFAULT_RETRY_CONFIG
        # schedule echoes every weekday key with ISO-serialized times.
        for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
            assert data["schedule"][day] == [{"start": "00:00:00", "end": "23:59:00"}]
        # Campaign row exists and N=3 QUEUED calls were seeded.
        async with deps.pools.api.acquire() as conn:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM calls WHERE campaign_id = $1", data["id"]
            )
            queued = await conn.fetchval(
                "SELECT COUNT(*) FROM calls WHERE campaign_id = $1 AND status = 'QUEUED'",
                data["id"],
            )
        assert total == 3
        assert queued == 3

    async def test_post_default_max_concurrent(self, client: AsyncClient, deps: Deps) -> None:
        resp = await client.post("/campaigns", json=_valid_campaign_body())
        assert resp.status_code == 201, resp.text
        assert resp.json()["max_concurrent"] == deps.settings.max_concurrent_default

    async def test_post_explicit_max_concurrent(self, client: AsyncClient) -> None:
        resp = await client.post("/campaigns", json=_valid_campaign_body(max_concurrent=9))
        assert resp.status_code == 201, resp.text
        assert resp.json()["max_concurrent"] == 9

    async def test_post_invalid_phone_422_with_indices(self, client: AsyncClient) -> None:
        body = _valid_campaign_body(phones=["+14155551234", "not-a-phone", "12345"])
        resp = await client.post("/campaigns", json=body)
        assert resp.status_code == 422, resp.text
        # The structured payload lives at ctx.invalid_phones so the frontend
        # can render per-line errors without regex-parsing a message string.
        detail = resp.json()["detail"]
        assert isinstance(detail, list)
        assert detail[0]["type"] == "invalid_phones"
        invalid = detail[0]["ctx"]["invalid_phones"]
        indices = {item["index"] for item in invalid}
        assert indices == {1, 2}

    async def test_post_zero_valid_phones_after_normalization(self, client: AsyncClient) -> None:
        resp = await client.post("/campaigns", json=_valid_campaign_body(phones=["not-a-phone"]))
        assert resp.status_code == 422, resp.text

    async def test_post_invalid_timezone_422(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/campaigns", json=_valid_campaign_body(timezone="Nowhere/Nowhere")
        )
        assert resp.status_code == 422, resp.text
        assert "unknown timezone" in resp.text

    async def test_post_invalid_schedule_window_422(self, client: AsyncClient) -> None:
        bad_schedule = dict(DEFAULT_SCHEDULE)
        bad_schedule["mon"] = [{"start": "10:00", "end": "09:00"}]
        resp = await client.post("/campaigns", json=_valid_campaign_body(schedule=bad_schedule))
        assert resp.status_code == 422, resp.text

    async def test_post_empty_phones_422(self, client: AsyncClient) -> None:
        resp = await client.post("/campaigns", json=_valid_campaign_body(phones=[]))
        assert resp.status_code == 422, resp.text

    async def test_post_missing_name_422(self, client: AsyncClient) -> None:
        body = _valid_campaign_body()
        del body["name"]
        resp = await client.post("/campaigns", json=body)
        assert resp.status_code == 422, resp.text

    async def test_post_triggers_wake_notify(self, client: AsyncClient, deps: Deps) -> None:
        # Start from a cleared wake so the test observes the POST-driven notify.
        deps.wake.clear()
        resp = await client.post("/campaigns", json=_valid_campaign_body())
        assert resp.status_code == 201, resp.text
        # The wake should already be set; wait with a short timeout to be safe.
        fired = await deps.wake.wait(timeout=0.5)
        assert fired is True


class TestGetList:
    async def test_get_list_paginated(self, client: AsyncClient) -> None:
        for i in range(3):
            body = _valid_campaign_body(
                name=f"list-{i}",
                phones=[f"+1415555{i:04d}"],
            )
            resp = await client.post("/campaigns", json=body)
            assert resp.status_code == 201, resp.text

        page1 = await client.get("/campaigns", params={"limit": 2})
        assert page1.status_code == 200, page1.text
        data1 = page1.json()
        assert len(data1["campaigns"]) == 2
        assert data1["next_cursor"] is not None

        page2 = await client.get("/campaigns", params={"limit": 2, "cursor": data1["next_cursor"]})
        assert page2.status_code == 200, page2.text
        data2 = page2.json()
        # Only 1 remaining row so the cursor exhausts.
        assert len(data2["campaigns"]) == 1
        assert data2["next_cursor"] is None

        # All ids unique across pages.
        ids = [c["id"] for c in data1["campaigns"] + data2["campaigns"]]
        assert len(set(ids)) == 3

    async def test_get_list_default_limit(self, client: AsyncClient) -> None:
        resp = await client.get("/campaigns")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "campaigns" in data
        assert "next_cursor" in data


class TestGetDetail:
    async def test_get_detail_returns_campaign(self, client: AsyncClient) -> None:
        body = _valid_campaign_body(name="detail", phones=["+14155557777"])
        created = await client.post("/campaigns", json=body)
        assert created.status_code == 201, created.text
        campaign_id = created.json()["id"]

        resp = await client.get(f"/campaigns/{campaign_id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == campaign_id
        assert resp.json()["name"] == "detail"

    async def test_get_detail_returns_404_missing(self, client: AsyncClient) -> None:
        resp = await client.get(f"/campaigns/{uuid4()}")
        assert resp.status_code == 404, resp.text


class TestGetStats:
    async def test_get_stats_returns_initial_counts(self, client: AsyncClient) -> None:
        body = _valid_campaign_body(
            name="stats", phones=["+14155558001", "+14155558002", "+14155558003"]
        )
        created = await client.post("/campaigns", json=body)
        assert created.status_code == 201, created.text
        campaign_id = created.json()["id"]

        resp = await client.get(f"/campaigns/{campaign_id}/stats")
        assert resp.status_code == 200, resp.text
        stats = resp.json()
        # All three rows are QUEUED, which counts as in_progress per the repo
        # (in_progress = QUEUED | DIALING | IN_PROGRESS | RETRY_PENDING).
        assert stats == {
            "total": 3,
            "completed": 0,
            "failed": 0,
            "retries_attempted": 0,
            "in_progress": 3,
        }

    async def test_get_stats_404_missing(self, client: AsyncClient) -> None:
        resp = await client.get(f"/campaigns/{uuid4()}/stats")
        assert resp.status_code == 404, resp.text


class TestGetCampaignCalls:
    # `GET /campaigns/{id}/calls` powers the drill-in drawer that surfaces
    # the per-call lifecycle. Contract: paginated with keyset cursor on
    # (updated_at DESC, id DESC), returns INTERNAL call-status vocabulary
    # (the drawer's raison d'etre is distinguishing QUEUED vs DIALING vs
    # RETRY_PENDING), 404s on an unknown campaign.

    async def test_returns_calls_in_updated_at_desc(self, client: AsyncClient) -> None:
        body = _valid_campaign_body(phones=["+14155557001", "+14155557002", "+14155557003"])
        created = await client.post("/campaigns", json=body)
        campaign_id = created.json()["id"]

        resp = await client.get(f"/campaigns/{campaign_id}/calls")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert "calls" in payload
        assert "next_cursor" in payload
        assert len(payload["calls"]) == 3
        # Shape: no provider_call_id (intentionally dropped — telephony
        # vocabulary doesn't belong on the campaigns API).
        first = payload["calls"][0]
        assert set(first.keys()) == {
            "id",
            "phone",
            "status",
            "attempt_epoch",
            "retries_remaining",
            "next_attempt_at",
            "updated_at",
        }
        # Status is internal vocabulary so the drawer can show QUEUED.
        for call in payload["calls"]:
            assert call["status"] == "QUEUED"
            assert call["attempt_epoch"] == 0
        # updated_at DESC: every row's updated_at >= the next row's.
        updated_ats = [c["updated_at"] for c in payload["calls"]]
        assert updated_ats == sorted(updated_ats, reverse=True)

    async def test_404_on_unknown_campaign(self, client: AsyncClient) -> None:
        resp = await client.get(f"/campaigns/{uuid4()}/calls")
        assert resp.status_code == 404, resp.text

    async def test_cursor_round_trip_pages_disjoint_and_cover_all(
        self, client: AsyncClient
    ) -> None:
        # Seed 5 calls, page with limit=2 → two pages of 2, third page of 1.
        # Cursor-paginated results must be strictly disjoint and their union
        # must equal the full `limit=big` single-page result — that's the
        # keyset contract we rely on in the drawer UI.
        phones = [f"+1415555{7100 + i:04d}" for i in range(5)]
        body = _valid_campaign_body(phones=phones)
        created = await client.post("/campaigns", json=body)
        campaign_id = created.json()["id"]

        full = await client.get(f"/campaigns/{campaign_id}/calls", params={"limit": 10})
        full_ids = {c["id"] for c in full.json()["calls"]}
        assert len(full_ids) == 5

        collected: set[str] = set()
        cursor: str | None = None
        pages = 0
        while True:
            params: dict[str, Any] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            resp = await client.get(f"/campaigns/{campaign_id}/calls", params=params)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            pages += 1
            page_ids = {c["id"] for c in data["calls"]}
            # Strictly disjoint across pages — keyset must never repeat.
            assert not (collected & page_ids), "keyset pagination returned duplicates"
            collected.update(page_ids)
            cursor = data["next_cursor"]
            if cursor is None:
                break
            # Guard against an infinite loop if the cursor ever stops advancing.
            assert pages < 10
        assert collected == full_ids

    async def test_empty_campaign_returns_empty_list(self, client: AsyncClient, deps: Deps) -> None:
        # A campaign with zero calls (theoretical; not reachable through
        # POST /campaigns because `phones` has min_length=1) still resolves
        # cleanly through the endpoint rather than 500-ing. We insert
        # directly via the repo to simulate the edge.
        async with deps.pools.scheduler.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO campaigns (name, status, timezone, schedule,
                    max_concurrent, retry_config)
                VALUES ('empty', 'PENDING', 'UTC',
                    '{"mon":[]}'::jsonb, 1, '{"max_attempts": 0,
                    "backoff_base_seconds": 1}'::jsonb)
                RETURNING id
                """
            )
        empty_id = row["id"]
        resp = await client.get(f"/campaigns/{empty_id}/calls")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload == {"calls": [], "next_cursor": None}
