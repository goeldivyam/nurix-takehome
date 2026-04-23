from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from app.config import Settings
from app.persistence.repositories import WebhookInboxRepo
from app.provider.mock import MockProvider, parse_event, verify_signature
from app.scheduler.reclaim import ReclaimKind, stuck_reclaim_sweep
from app.scheduler.wake import SchedulerWake
from app.scheduler.webhook_processor import _process_one_row, process_pending_inbox
from app.state.types import CallStatus

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"


# -- fixtures ----------------------------------------------------------------


@pytest.fixture(scope="module")
def pg() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as c:
        yield c


@pytest.fixture
async def pool(pg: PostgresContainer) -> AsyncIterator[asyncpg.Pool]:
    # Re-apply the schema every test — cheaper than per-test truncation of
    # 5 tables and keeps test ordering irrelevant.
    dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    assert p is not None
    async with p.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.execute(SCHEMA_PATH.read_text())
    yield p
    await p.close()


@pytest.fixture
async def deps(pool: asyncpg.Pool) -> AsyncIterator[Any]:
    # Settings tuned for fast reclaim: max_call_duration_seconds = 10 so the
    # grace window is 40s. Tests write `updated_at = NOW() - 10 minutes` to
    # force a row past the grace window.
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        max_call_duration_seconds=10,
        stuck_reclaim_get_status_timeout_seconds=1,
        webhook_processor_batch_max=50,
    )
    wake = SchedulerWake()

    # The MockProvider wants an event sink — for these tests we don't exercise
    # the sink; we seed inbox rows directly. Keep the sink a no-op.
    async def noop_sink(_payload: dict[str, Any]) -> None:
        return None

    provider = MockProvider(settings, noop_sink)

    # Re-use the single asyncpg pool as all three pools so the container
    # stays under a sensible connection cap. In production these are
    # separate pools to isolate workloads.
    pools = SimpleNamespace(api=pool, scheduler=pool, webhook=pool)
    yield SimpleNamespace(
        settings=settings,
        pools=pools,
        provider=provider,
        wake=wake,
        parse_event_fn=parse_event,
        verify_signature_fn=verify_signature,
    )
    await provider.aclose()


# -- seeding helpers ---------------------------------------------------------


async def _seed_campaign(
    pool: asyncpg.Pool,
    *,
    status: str = "ACTIVE",
    name: str = "campaign",
) -> UUID:
    async with pool.acquire() as conn:
        return await conn.fetchval(  # type: ignore[no-any-return]
            """
            INSERT INTO campaigns (name, status, timezone, schedule, max_concurrent, retry_config)
            VALUES ($1, $2, 'UTC', '{}'::jsonb, 3, '{}'::jsonb)
            RETURNING id
            """,
            name,
            status,
        )


async def _seed_dialing_call(
    pool: asyncpg.Pool,
    campaign_id: UUID,
    *,
    phone: str,
    attempt_epoch: int = 1,
    provider_call_id: str | None = None,
    stale_by_seconds: int = 600,
) -> UUID:
    # Insert a DIALING row with `updated_at` in the past so the stuck-reclaim
    # predicate (`updated_at < NOW() - grace`) picks it up.
    async with pool.acquire() as conn:
        call_id: UUID = await conn.fetchval(
            """
            INSERT INTO calls
                (campaign_id, phone, status, attempt_epoch, retries_remaining,
                 provider_call_id, updated_at)
            VALUES ($1, $2, 'DIALING', $3, 2, $4,
                    NOW() - make_interval(secs => $5))
            RETURNING id
            """,
            campaign_id,
            phone,
            attempt_epoch,
            provider_call_id,
            stale_by_seconds,
        )
        return call_id


async def _audit_rows(
    pool: asyncpg.Pool,
    *,
    event_type: str | None = None,
    call_id: UUID | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if event_type is not None:
        params.append(event_type)
        clauses.append(f"event_type = ${len(params)}")
    if call_id is not None:
        params.append(call_id)
        clauses.append(f"call_id = ${len(params)}")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type, campaign_id, call_id, state_before, "  # noqa: S608
            "state_after, extra, reason "
            f"FROM scheduler_audit {where} ORDER BY id",
            *params,
        )
    return [dict(r) for r in rows]


async def _get_call(pool: asyncpg.Pool, call_id: UUID) -> dict[str, Any]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM calls WHERE id = $1", call_id)
        assert row is not None
        return dict(row)


# -- tests -------------------------------------------------------------------


class TestStuckReclaim:
    async def test_stuck_dialing_is_reclaimed(self, deps: Any, pool: asyncpg.Pool) -> None:
        # Null provider_call_id + updated_at older than grace → reclaim branch
        # runs; epoch bumps and row returns to QUEUED.
        campaign_id = await _seed_campaign(pool)
        call_id = await _seed_dialing_call(
            pool,
            campaign_id,
            phone="+14155550001",
            attempt_epoch=1,
            provider_call_id=None,
            stale_by_seconds=600,
        )

        outcomes = await stuck_reclaim_sweep(deps)
        assert len(outcomes) == 1
        assert outcomes[0].call_id == call_id
        assert outcomes[0].kind is ReclaimKind.EXECUTED

        row = await _get_call(pool, call_id)
        assert row["status"] == "QUEUED"
        assert row["attempt_epoch"] == 2

        reclaim_audits = await _audit_rows(pool, event_type="RECLAIM_EXECUTED", call_id=call_id)
        assert len(reclaim_audits) == 1
        assert reclaim_audits[0]["state_before"] == "DIALING"
        assert reclaim_audits[0]["state_after"] == "QUEUED"

    async def test_terminal_apply_on_provider_confirmed_terminal(
        self, deps: Any, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        campaign_id = await _seed_campaign(pool)
        call_id = await _seed_dialing_call(
            pool,
            campaign_id,
            phone="+14155550002",
            attempt_epoch=1,
            provider_call_id="pc-terminal-1",
            stale_by_seconds=600,
        )

        # Override the provider's get_status to confirm COMPLETED — mimics
        # a call that finished but whose webhook was lost.
        async def fake_get_status(_pc: str) -> CallStatus:
            return CallStatus.COMPLETED

        monkeypatch.setattr(deps.provider, "get_status", fake_get_status)

        outcomes = await stuck_reclaim_sweep(deps)
        assert len(outcomes) == 1
        assert outcomes[0].kind is ReclaimKind.TERMINAL_APPLIED
        assert outcomes[0].detail == "COMPLETED"

        row = await _get_call(pool, call_id)
        assert row["status"] == "COMPLETED"
        # No epoch bump on terminal-apply.
        assert row["attempt_epoch"] == 1

        terminal_audits = await _audit_rows(
            pool, event_type="RECLAIM_SKIPPED_TERMINAL", call_id=call_id
        )
        assert len(terminal_audits) == 1

    async def test_sweep_empty_when_nothing_stuck(self, deps: Any, pool: asyncpg.Pool) -> None:
        # DIALING row with a fresh updated_at must NOT be reclaimed.
        campaign_id = await _seed_campaign(pool)
        async with pool.acquire() as conn:
            call_id: UUID = await conn.fetchval(
                """
                INSERT INTO calls
                    (campaign_id, phone, status, attempt_epoch, retries_remaining)
                VALUES ($1, '+14155550003', 'DIALING', 1, 2)
                RETURNING id
                """,
                campaign_id,
            )

        outcomes = await stuck_reclaim_sweep(deps)
        assert outcomes == []

        row = await _get_call(pool, call_id)
        assert row["status"] == "DIALING"
        assert row["attempt_epoch"] == 1


class TestWebhookProcessor:
    async def test_applies_transition_and_marks_inbox_processed(
        self, deps: Any, pool: asyncpg.Pool
    ) -> None:
        campaign_id = await _seed_campaign(pool)
        async with pool.acquire() as conn:
            call_id: UUID = await conn.fetchval(
                """
                INSERT INTO calls
                    (campaign_id, phone, status, attempt_epoch, retries_remaining,
                     provider_call_id)
                VALUES ($1, '+14155550010', 'DIALING', 1, 2, 'pc-1')
                RETURNING id
                """,
                campaign_id,
            )

        payload = {
            "provider_event_id": "pc-1:1",
            "provider_call_id": "pc-1",
            "status": "IN_PROGRESS",
        }
        async with pool.acquire() as conn:
            inbox_id = await WebhookInboxRepo.insert(
                conn,
                provider="mock",
                provider_event_id=payload["provider_event_id"],
                payload=payload,
                headers={},
            )

        processed = await process_pending_inbox(deps)
        assert processed == 1

        call = await _get_call(pool, call_id)
        assert call["status"] == "IN_PROGRESS"
        assert call["attempt_epoch"] == 1  # terminal-ish but non-reclaim, no bump

        async with pool.acquire() as conn:
            inbox_row = await conn.fetchrow(
                "SELECT processed_at FROM webhook_inbox WHERE id = $1", inbox_id
            )
        assert inbox_row is not None
        assert inbox_row["processed_at"] is not None

        transition_audits = await _audit_rows(pool, event_type="TRANSITION", call_id=call_id)
        assert len(transition_audits) == 1
        assert transition_audits[0]["state_before"] == "DIALING"
        assert transition_audits[0]["state_after"] == "IN_PROGRESS"

    async def test_ignores_unknown_provider_call_id(self, deps: Any, pool: asyncpg.Pool) -> None:
        async with pool.acquire() as conn:
            inbox_id = await WebhookInboxRepo.insert(
                conn,
                provider="mock",
                provider_event_id="pc-unknown:1",
                payload={
                    "provider_event_id": "pc-unknown:1",
                    "provider_call_id": "pc-unknown",
                    "status": "COMPLETED",
                },
                headers={},
            )

        processed = await process_pending_inbox(deps)
        assert processed == 1

        async with pool.acquire() as conn:
            inbox_row = await conn.fetchrow(
                "SELECT processed_at FROM webhook_inbox WHERE id = $1", inbox_id
            )
        assert inbox_row is not None
        assert inbox_row["processed_at"] is not None

        stale_audits = await _audit_rows(pool, event_type="WEBHOOK_IGNORED_STALE")
        assert len(stale_audits) == 1
        extra = (
            json.loads(stale_audits[0]["extra"])
            if isinstance(stale_audits[0]["extra"], str)
            else dict(stale_audits[0]["extra"])
        )
        assert extra["provider_call_id"] == "pc-unknown"

    async def test_reclaim_then_redial_retroactive_event_is_unknown_and_noops(
        self, deps: Any, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end invariant from the plan: a retroactive provider event
        # for an ORIGINAL epoch's provider_call_id must never clobber a
        # live row after the reclaim+redial cycle has moved on.
        #
        # The key insight is that `place_call(idem_key)` where idem_key =
        # f"{call_id}:{attempt_epoch}" produces a NEW provider_call_id on
        # re-dial. The subsequent DISPATCH overwrites `calls.provider_call_id`
        # with the new id, so a late webhook for the OLD id finds no row
        # and CAS-no-ops via the "unknown provider_call_id" path — the
        # same code path a never-seen event would take.

        # Step 1 — seed a stuck DIALING row at epoch=1, pc=pc-epoch-1.
        campaign_id = await _seed_campaign(pool)
        call_id = await _seed_dialing_call(
            pool,
            campaign_id,
            phone="+14155550020",
            attempt_epoch=1,
            provider_call_id="pc-epoch-1",
            stale_by_seconds=600,
        )

        # Force reclaim to take the reclaim branch by making get_status
        # hang past the configured timeout.
        async def hanging_get_status(_pc: str) -> CallStatus:
            await _never()
            return CallStatus.COMPLETED

        monkeypatch.setattr(deps.provider, "get_status", hanging_get_status)

        outcomes = await stuck_reclaim_sweep(deps)
        assert [o.kind for o in outcomes] == [ReclaimKind.EXECUTED]

        # Step 2 — simulate the next claim + re-dial landing a NEW
        # provider_call_id on the row (what Phase 3 of the scheduler
        # tick does via state.transition + column_updates).
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE calls
                SET status = 'DIALING', attempt_epoch = 3,
                    provider_call_id = 'pc-epoch-3', updated_at = NOW()
                WHERE id = $1
                """,
                call_id,
            )

        # Step 3 — insert a retroactive webhook for the ORIGINAL
        # provider_call_id carrying a COMPLETED status. This is exactly
        # the "classic lost webhook that finally arrived" scenario.
        async with pool.acquire() as conn:
            await WebhookInboxRepo.insert(
                conn,
                provider="mock",
                provider_event_id="pc-epoch-1:3",
                payload={
                    "provider_event_id": "pc-epoch-1:3",
                    "provider_call_id": "pc-epoch-1",
                    "status": "COMPLETED",
                },
                headers={},
            )

        processed = await process_pending_inbox(deps)
        assert processed == 1

        # The call row is unchanged — the late event never found a call
        # to mutate. Observability: a WEBHOOK_IGNORED_STALE row with
        # "unknown provider_call_id" reason was emitted.
        row_after = await _get_call(pool, call_id)
        assert row_after["status"] == "DIALING"
        assert row_after["attempt_epoch"] == 3
        assert row_after["provider_call_id"] == "pc-epoch-3"

        stale_audits = await _audit_rows(pool, event_type="WEBHOOK_IGNORED_STALE")
        assert len(stale_audits) == 1
        assert stale_audits[0]["reason"] == "unknown provider_call_id"
        extra = (
            json.loads(stale_audits[0]["extra"])
            if isinstance(stale_audits[0]["extra"], str)
            else dict(stale_audits[0]["extra"])
        )
        assert extra["provider_call_id"] == "pc-epoch-1"
        assert extra["provider_event_id"] == "pc-epoch-1:3"

    async def test_one_txn_per_row_rolls_back_atomically(
        self, deps: Any, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Monkeypatch mark_processed to raise AFTER the state.transition CAS
        # has run. The whole txn must roll back: call status stays DIALING
        # AND inbox processed_at stays NULL.
        campaign_id = await _seed_campaign(pool)
        async with pool.acquire() as conn:
            call_id: UUID = await conn.fetchval(
                """
                INSERT INTO calls
                    (campaign_id, phone, status, attempt_epoch, retries_remaining,
                     provider_call_id)
                VALUES ($1, '+14155550030', 'DIALING', 1, 2, 'pc-rollback')
                RETURNING id
                """,
                campaign_id,
            )

            inbox_id = await WebhookInboxRepo.insert(
                conn,
                provider="mock",
                provider_event_id="pc-rollback:1",
                payload={
                    "provider_event_id": "pc-rollback:1",
                    "provider_call_id": "pc-rollback",
                    "status": "IN_PROGRESS",
                },
                headers={},
            )

        from app.scheduler import webhook_processor as wp

        async def exploding_mark_processed(_conn: Any, _inbox_id: UUID) -> None:
            raise RuntimeError("forced rollback")

        monkeypatch.setattr(wp.WebhookInboxRepo, "mark_processed", exploding_mark_processed)

        outcome = await _process_one_row(deps)
        assert outcome == "error"

        # Both mutations rolled back — call still DIALING, inbox still NULL.
        call = await _get_call(pool, call_id)
        assert call["status"] == "DIALING"
        assert call["attempt_epoch"] == 1

        async with pool.acquire() as conn:
            inbox_row = await conn.fetchrow(
                "SELECT processed_at FROM webhook_inbox WHERE id = $1", inbox_id
            )
        assert inbox_row is not None
        assert inbox_row["processed_at"] is None

        # And the TRANSITION audit that would have been written in the same
        # txn is also rolled back — nothing in the scheduler_audit table.
        transition_audits = await _audit_rows(pool, event_type="TRANSITION", call_id=call_id)
        assert transition_audits == []


# -- utilities ---------------------------------------------------------------


async def _never() -> None:
    # Helper that awaits forever; paired with wait_for to force a
    # TimeoutError deterministically without a real sleep.
    import asyncio

    await asyncio.Event().wait()
