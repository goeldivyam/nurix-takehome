from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="module")
def _pg() -> AsyncIterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as c:
        yield c


@pytest.fixture
async def running_app(
    _pg: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    dsn = _pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    # Prepare the schema ONCE against the container before the app's
    # lifespan tries to open its pools.
    prep = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    assert prep is not None
    async with prep.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.execute(Path("schema.sql").read_text())
    await prep.close()

    # Point Settings at the container + shrink durations so the daemons
    # idle-loop quickly and the shutdown path joins in milliseconds.
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("SCHEDULER_SAFETY_NET_SECONDS", "0.1")
    monkeypatch.setenv("RECLAIM_SWEEP_INTERVAL_SECONDS", "0.5")
    monkeypatch.setenv("MOCK_CALL_DURATION_SECONDS", "0.05")
    monkeypatch.setenv("DEBUG_ENDPOINTS_ENABLED", "false")

    # Import app fresh so the module-level Settings() for debug flags
    # picks up the monkeypatched env.
    import importlib

    import app.main as main_module

    importlib.reload(main_module)

    app_obj = main_module.app
    # httpx's ASGITransport does NOT run the lifespan protocol; we drive
    # it manually via the app's lifespan_context so daemons and pools are
    # actually up before the first request.
    async with (
        app_obj.router.lifespan_context(app_obj),
        AsyncClient(
            transport=ASGITransport(app=app_obj),
            base_url="http://test",
        ) as ac,
    ):
        yield ac


class TestHealth:
    async def test_health_reflects_pool_sizes(self, running_app: AsyncClient) -> None:
        resp = await running_app.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["pools"] is not None
        for role in ("api", "scheduler", "webhook"):
            assert data["pools"][role]["size"] >= 0
            assert data["pools"][role]["idle"] >= 0

    async def test_openapi_includes_all_routers(self, running_app: AsyncClient) -> None:
        resp = await running_app.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        paths = set(spec["paths"].keys())
        # Every router registered by P3A and P3B is reachable.
        assert "/campaigns" in paths
        assert "/campaigns/{campaign_id}" in paths
        assert "/campaigns/{campaign_id}/stats" in paths
        assert "/calls/{call_id}" in paths
        assert "/audit" in paths
        assert "/webhooks/provider" in paths
        assert "/health" in paths


class TestLifespanShutdown:
    async def test_tracked_tasks_drained_on_shutdown(
        self,
        _pg: PostgresContainer,  # noqa: PT019 -- DSN extraction requires the value
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # We need the container's DSN, not just its availability — so the
        # fixture is injected by parameter rather than `usefixtures`.
        dsn = _pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        prep = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
        assert prep is not None
        async with prep.acquire() as conn:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
            await conn.execute(Path("schema.sql").read_text())
        await prep.close()

        monkeypatch.setenv("DATABASE_URL", dsn)
        monkeypatch.setenv("SCHEDULER_SAFETY_NET_SECONDS", "0.05")
        monkeypatch.setenv("RECLAIM_SWEEP_INTERVAL_SECONDS", "0.05")

        import importlib

        import app.main as main_module

        importlib.reload(main_module)
        app_obj = main_module.app

        async with (
            app_obj.router.lifespan_context(app_obj),
            AsyncClient(
                transport=ASGITransport(app=app_obj),
                base_url="http://test",
            ) as ac,
        ):
            await ac.get("/health")
            tracked = app_obj.state.tracked_tasks
            assert len(tracked) == 3  # scheduler, reclaim, inbox-safety
            # Give daemons one idle cycle so their done-callbacks wire up.
            await asyncio.sleep(0.1)

        # Exiting the lifespan context runs shutdown. Every tracked task
        # should be cancelled + joined by the time we land here.
        assert len(app_obj.state.tracked_tasks) == 0
