from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import asyncpg

from app.audit.events import AuditEvent

# -- Row types ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CampaignRow:
    id: UUID
    name: str
    status: str
    timezone: str
    schedule: dict[str, Any]
    max_concurrent: int
    retry_config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CampaignRowWithCursor:
    id: UUID
    name: str
    status: str
    timezone: str
    schedule: dict[str, Any]
    max_concurrent: int
    retry_config: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    last_dispatch_at: datetime | None


@dataclass(frozen=True, slots=True)
class CampaignStats:
    total: int
    completed: int
    failed: int
    retries_attempted: int
    in_progress: int


@dataclass(frozen=True, slots=True)
class CallRow:
    id: UUID
    campaign_id: UUID
    phone: str
    status: str
    attempt_epoch: int
    retries_remaining: int
    next_attempt_at: datetime | None
    provider_call_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AuditRow:
    id: int
    ts: datetime
    event_type: str
    campaign_id: UUID | None
    call_id: UUID | None
    reason: str
    state_before: str | None
    state_after: str | None
    extra: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WebhookInboxRow:
    id: UUID
    provider: str
    provider_event_id: str
    payload: dict[str, Any]
    headers: dict[str, Any]
    received_at: datetime
    processed_at: datetime | None


@dataclass(frozen=True, slots=True)
class TerminalAggregate:
    completed: int
    failed: int
    no_answer: int
    busy: int


# -- Cursor helpers ----------------------------------------------------------

AUDIT_LIST_MAX_LIMIT = 500


def _encode_audit_cursor(ts: datetime, row_id: int) -> str:
    payload = json.dumps({"ts": ts.isoformat(), "id": row_id}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_audit_cursor(cursor: str) -> tuple[datetime, int]:
    raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
    obj = json.loads(raw.decode("utf-8"))
    ts = datetime.fromisoformat(obj["ts"])
    return ts, int(obj["id"])


def _encode_campaign_cursor(created_at: datetime, campaign_id: UUID) -> str:
    payload = json.dumps({"created_at": created_at.isoformat(), "id": str(campaign_id)}).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_campaign_cursor(cursor: str) -> tuple[datetime, UUID]:
    raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
    obj = json.loads(raw.decode("utf-8"))
    created_at = datetime.fromisoformat(obj["created_at"])
    return created_at, UUID(obj["id"])


# -- JSON decode helpers -----------------------------------------------------


def _loads_json(value: Any) -> dict[str, Any]:
    # JSONB columns come back as str from asyncpg unless a codec is registered.
    # The repo layer decodes explicitly — simpler than per-pool codec setup and
    # keeps the connection-pool story uniform across test / prod.
    if value is None:
        return {}
    if isinstance(value, str):
        return cast(dict[str, Any], json.loads(value))
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    raise TypeError(f"unexpected JSONB value type: {type(value)!r}")


def _dumps_json(value: dict[str, Any]) -> str:
    return json.dumps(value)


# -- CampaignRepo ------------------------------------------------------------


class CampaignRepo:
    @staticmethod
    async def create(
        conn: asyncpg.Connection,
        *,
        name: str,
        timezone: str,
        schedule: dict[str, Any],
        max_concurrent: int,
        retry_config: dict[str, Any],
    ) -> UUID:
        row = await conn.fetchrow(
            """
            INSERT INTO campaigns
                (name, status, timezone, schedule, max_concurrent, retry_config)
            VALUES ($1, 'PENDING', $2, $3::jsonb, $4, $5::jsonb)
            RETURNING id
            """,
            name,
            timezone,
            _dumps_json(schedule),
            max_concurrent,
            _dumps_json(retry_config),
        )
        if row is None:
            raise RuntimeError("campaign insert returned no row")
        return cast(UUID, row["id"])

    @staticmethod
    async def get(conn: asyncpg.Connection, campaign_id: UUID) -> CampaignRow | None:
        row = await conn.fetchrow(
            """
            SELECT id, name, status, timezone, schedule, max_concurrent,
                   retry_config, created_at, updated_at
            FROM campaigns
            WHERE id = $1
            """,
            campaign_id,
        )
        if row is None:
            return None
        return CampaignRow(
            id=row["id"],
            name=row["name"],
            status=row["status"],
            timezone=row["timezone"],
            schedule=_loads_json(row["schedule"]),
            max_concurrent=row["max_concurrent"],
            retry_config=_loads_json(row["retry_config"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    async def list_page(
        api_pool: asyncpg.Pool,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[CampaignRow], str | None]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if cursor is None:
            rows = await api_pool.fetch(
                """
                SELECT id, name, status, timezone, schedule, max_concurrent,
                       retry_config, created_at, updated_at
                FROM campaigns
                ORDER BY created_at DESC, id DESC
                LIMIT $1
                """,
                limit,
            )
        else:
            cursor_ts, cursor_id = _decode_campaign_cursor(cursor)
            rows = await api_pool.fetch(
                """
                SELECT id, name, status, timezone, schedule, max_concurrent,
                       retry_config, created_at, updated_at
                FROM campaigns
                WHERE (created_at, id) < ($1, $2)
                ORDER BY created_at DESC, id DESC
                LIMIT $3
                """,
                cursor_ts,
                cursor_id,
                limit,
            )
        result = [
            CampaignRow(
                id=r["id"],
                name=r["name"],
                status=r["status"],
                timezone=r["timezone"],
                schedule=_loads_json(r["schedule"]),
                max_concurrent=r["max_concurrent"],
                retry_config=_loads_json(r["retry_config"]),
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]
        next_cursor: str | None = None
        if len(result) == limit and result:
            last = result[-1]
            next_cursor = _encode_campaign_cursor(last.created_at, last.id)
        return result, next_cursor

    @staticmethod
    async def list_eligible_for_tick(
        conn: asyncpg.Connection,
    ) -> list[CampaignRowWithCursor]:
        rows = await conn.fetch(
            """
            SELECT c.id, c.name, c.status, c.timezone, c.schedule,
                   c.max_concurrent, c.retry_config, c.created_at, c.updated_at,
                   s.last_dispatch_at
            FROM campaigns c
            LEFT JOIN scheduler_campaign_state s ON s.campaign_id = c.id
            WHERE c.status IN ('PENDING', 'ACTIVE')
            ORDER BY c.id ASC
            """
        )
        return [
            CampaignRowWithCursor(
                id=r["id"],
                name=r["name"],
                status=r["status"],
                timezone=r["timezone"],
                schedule=_loads_json(r["schedule"]),
                max_concurrent=r["max_concurrent"],
                retry_config=_loads_json(r["retry_config"]),
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                last_dispatch_at=r["last_dispatch_at"],
            )
            for r in rows
        ]

    @staticmethod
    async def stats(api_pool: asyncpg.Pool, campaign_id: UUID) -> CampaignStats:
        # Single aggregate pass. `retries_attempted` sums attempt_epoch across
        # every call row — each row's epoch counts the total attempts made for
        # that row, including the initial dial. `failed` aggregates every
        # terminal non-success (FAILED + NO_ANSWER + BUSY) so it matches the
        # external status mapping exposed by /calls/{id} — completed + failed
        # + in_progress always sum to total.
        row = await api_pool.fetchrow(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'COMPLETED') AS completed,
                COUNT(*) FILTER (WHERE status IN ('FAILED', 'NO_ANSWER', 'BUSY')) AS failed,
                COALESCE(SUM(attempt_epoch), 0) AS retries_attempted,
                COUNT(*) FILTER (
                    WHERE status IN ('QUEUED', 'DIALING', 'IN_PROGRESS', 'RETRY_PENDING')
                ) AS in_progress
            FROM calls
            WHERE campaign_id = $1
            """,
            campaign_id,
        )
        if row is None:
            return CampaignStats(0, 0, 0, 0, 0)
        return CampaignStats(
            total=int(row["total"]),
            completed=int(row["completed"]),
            failed=int(row["failed"]),
            retries_attempted=int(row["retries_attempted"]),
            in_progress=int(row["in_progress"]),
        )

    @staticmethod
    async def transition_if(
        conn: asyncpg.Connection,
        campaign_id: UUID,
        expected_status: str,
        new_status: str,
    ) -> bool:
        row = await conn.fetchrow(
            """
            UPDATE campaigns
            SET status = $2, updated_at = NOW()
            WHERE id = $1 AND status = $3
            RETURNING id
            """,
            campaign_id,
            new_status,
            expected_status,
        )
        return row is not None


# -- CallRepo ----------------------------------------------------------------


class CallRepo:
    @staticmethod
    async def create_batch(
        conn: asyncpg.Connection,
        *,
        campaign_id: UUID,
        phones: list[str],
        retries_remaining: int,
    ) -> list[UUID]:
        if not phones:
            return []
        # WITH ORDINALITY preserves input order so the returned ids line up with
        # the phones list the caller passed in.
        rows = await conn.fetch(
            """
            INSERT INTO calls (campaign_id, phone, status, retries_remaining)
            SELECT $1, phone, 'QUEUED', $2
            FROM UNNEST($3::text[]) WITH ORDINALITY AS t(phone, ord)
            ORDER BY t.ord
            RETURNING id
            """,
            campaign_id,
            retries_remaining,
            phones,
        )
        return [cast(UUID, r["id"]) for r in rows]

    @staticmethod
    async def claim_next_queued(
        conn: asyncpg.Connection,
        campaign_id: UUID,
    ) -> CallRow | None:
        # Atomic claim primitive — see backend-conventions skill. SKIP LOCKED
        # makes concurrent claimers get different rows instead of serializing.
        # attempt_epoch is bumped here so the idempotency key the caller hands
        # to the provider is unique per dial attempt.
        row = await conn.fetchrow(
            """
            WITH candidate AS (
                SELECT id, attempt_epoch
                FROM calls
                WHERE campaign_id = $1
                  AND status = 'QUEUED'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE calls
            SET status = 'DIALING',
                attempt_epoch = calls.attempt_epoch + 1,
                updated_at = NOW()
            FROM candidate
            WHERE calls.id = candidate.id
            RETURNING calls.*
            """,
            campaign_id,
        )
        return _call_row_from_record(row) if row is not None else None

    @staticmethod
    async def find_retry_due_campaign_ids(conn: asyncpg.Connection) -> list[UUID]:
        rows = await conn.fetch(
            """
            SELECT DISTINCT campaign_id
            FROM calls
            WHERE status = 'RETRY_PENDING'
              AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
            """
        )
        return [cast(UUID, r["campaign_id"]) for r in rows]

    @staticmethod
    async def in_flight_count(conn: asyncpg.Connection, campaign_id: UUID) -> int:
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM calls
            WHERE campaign_id = $1 AND status IN ('DIALING', 'IN_PROGRESS')
            """,
            campaign_id,
        )
        return int(value or 0)

    @staticmethod
    async def in_flight_counts_by_campaign(
        conn: asyncpg.Connection,
        campaign_ids: list[UUID],
    ) -> dict[UUID, int]:
        # Single GROUP BY — the tick uses this once per invocation to avoid an
        # N+1 of per-campaign in-flight counts behind the concurrency gate.
        result: dict[UUID, int] = dict.fromkeys(campaign_ids, 0)
        if not campaign_ids:
            return result
        rows = await conn.fetch(
            """
            SELECT campaign_id, COUNT(*) AS n
            FROM calls
            WHERE campaign_id = ANY($1::uuid[])
              AND status IN ('DIALING', 'IN_PROGRESS')
            GROUP BY campaign_id
            """,
            campaign_ids,
        )
        for r in rows:
            result[cast(UUID, r["campaign_id"])] = int(r["n"])
        return result

    @staticmethod
    async def count_active_by_campaign(
        conn: asyncpg.Connection,
        campaign_id: UUID,
    ) -> int:
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM calls
            WHERE campaign_id = $1
              AND status IN ('QUEUED', 'DIALING', 'IN_PROGRESS', 'RETRY_PENDING')
            """,
            campaign_id,
        )
        return int(value or 0)

    @staticmethod
    async def count_retries_due_system(conn: asyncpg.Connection) -> int:
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM calls
            WHERE status = 'RETRY_PENDING'
              AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
            """
        )
        return int(value or 0)

    @staticmethod
    async def terminal_aggregate(
        conn: asyncpg.Connection,
        campaign_id: UUID,
    ) -> TerminalAggregate:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'COMPLETED') AS completed,
                COUNT(*) FILTER (WHERE status = 'FAILED') AS failed,
                COUNT(*) FILTER (WHERE status = 'NO_ANSWER') AS no_answer,
                COUNT(*) FILTER (WHERE status = 'BUSY') AS busy
            FROM calls
            WHERE campaign_id = $1
            """,
            campaign_id,
        )
        if row is None:
            return TerminalAggregate(0, 0, 0, 0)
        return TerminalAggregate(
            completed=int(row["completed"]),
            failed=int(row["failed"]),
            no_answer=int(row["no_answer"]),
            busy=int(row["busy"]),
        )

    @staticmethod
    async def find_stuck_dialing(
        conn: asyncpg.Connection,
        threshold_seconds: int,
    ) -> list[CallRow]:
        # `make_interval(secs => $1)` keeps the threshold parameter-bound —
        # never interpolate into SQL (flake8-bandit S608).
        rows = await conn.fetch(
            """
            SELECT *
            FROM calls
            WHERE status = 'DIALING'
              AND updated_at < NOW() - make_interval(secs => $1)
            """,
            threshold_seconds,
        )
        return [_call_row_from_record(r) for r in rows]

    @staticmethod
    async def get_by_provider_call_id(
        conn: asyncpg.Connection,
        provider_call_id: str,
    ) -> CallRow | None:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM calls
            WHERE provider_call_id = $1
            """,
            provider_call_id,
        )
        return _call_row_from_record(row) if row is not None else None

    @staticmethod
    async def get(conn: asyncpg.Connection, call_id: UUID) -> CallRow | None:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM calls
            WHERE id = $1
            """,
            call_id,
        )
        return _call_row_from_record(row) if row is not None else None


def _call_row_from_record(row: asyncpg.Record) -> CallRow:
    return CallRow(
        id=row["id"],
        campaign_id=row["campaign_id"],
        phone=row["phone"],
        status=row["status"],
        attempt_epoch=row["attempt_epoch"],
        retries_remaining=row["retries_remaining"],
        next_attempt_at=row["next_attempt_at"],
        provider_call_id=row["provider_call_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# -- AuditRepo ---------------------------------------------------------------


class AuditRepo:
    @staticmethod
    async def emit(conn: asyncpg.Connection, event: AuditEvent) -> None:
        await conn.execute(
            """
            INSERT INTO scheduler_audit
                (event_type, campaign_id, call_id, reason,
                 state_before, state_after, extra)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            """,
            event.event_type,
            event.campaign_id,
            event.call_id,
            event.reason,
            event.state_before,
            event.state_after,
            _dumps_json(event.extra),
        )

    @staticmethod
    async def list(
        api_pool: asyncpg.Pool,
        *,
        campaign_id: UUID | None = None,
        event_type: str | list[str] | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        reason_contains: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[list[AuditRow], str | None]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if limit > AUDIT_LIST_MAX_LIMIT:
            raise ValueError(f"limit must be <= {AUDIT_LIST_MAX_LIMIT}")

        clauses: list[str] = []
        args: list[Any] = []

        def bind(value: Any) -> str:
            args.append(value)
            return f"${len(args)}"

        if campaign_id is not None:
            clauses.append(f"campaign_id = {bind(campaign_id)}")
        if event_type is not None:
            if isinstance(event_type, list):
                clauses.append(f"event_type = ANY({bind(event_type)}::text[])")
            else:
                clauses.append(f"event_type = {bind(event_type)}")
        if from_ts is not None:
            clauses.append(f"ts >= {bind(from_ts)}")
        if to_ts is not None:
            clauses.append(f"ts <= {bind(to_ts)}")
        if reason_contains is not None:
            # ILIKE wildcards — escape % and _ in caller input, then wrap.
            escaped = reason_contains.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(f"reason ILIKE {bind(f'%{escaped}%')}")
        if cursor is not None:
            cursor_ts, cursor_id = _decode_audit_cursor(cursor)
            clauses.append(f"(ts, id) < ({bind(cursor_ts)}, {bind(cursor_id)})")

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_placeholder = bind(limit)

        # Clauses built from static fragments with only `$N` placeholders —
        # every user value travels via args.
        sql = (
            "SELECT id, ts, event_type, campaign_id, call_id, reason, "  # noqa: S608
            "state_before, state_after, extra "
            f"FROM scheduler_audit {where_sql} "
            f"ORDER BY ts DESC, id DESC LIMIT {limit_placeholder}"
        )

        rows = await api_pool.fetch(sql, *args)
        result = [
            AuditRow(
                id=r["id"],
                ts=r["ts"],
                event_type=r["event_type"],
                campaign_id=r["campaign_id"],
                call_id=r["call_id"],
                reason=r["reason"],
                state_before=r["state_before"],
                state_after=r["state_after"],
                extra=_loads_json(r["extra"]),
            )
            for r in rows
        ]
        next_cursor: str | None = None
        if len(result) == limit and result:
            last = result[-1]
            next_cursor = _encode_audit_cursor(last.ts, last.id)
        return result, next_cursor


# -- WebhookInboxRepo --------------------------------------------------------


class WebhookInboxRepo:
    @staticmethod
    async def insert(
        conn: asyncpg.Connection,
        provider: str,
        provider_event_id: str,
        payload: dict[str, Any],
        headers: dict[str, Any],
    ) -> UUID:
        # ON CONFLICT ... DO UPDATE SET provider = EXCLUDED.provider is the
        # standard trick to get RETURNING id to fire on the conflict path.
        # A plain DO NOTHING would return zero rows on duplicate insert.
        row = await conn.fetchrow(
            """
            INSERT INTO webhook_inbox
                (provider, provider_event_id, payload, headers)
            VALUES ($1, $2, $3::jsonb, $4::jsonb)
            ON CONFLICT (provider, provider_event_id)
            DO UPDATE SET provider = EXCLUDED.provider
            RETURNING id
            """,
            provider,
            provider_event_id,
            _dumps_json(payload),
            _dumps_json(headers),
        )
        if row is None:
            raise RuntimeError("webhook inbox insert returned no row")
        return cast(UUID, row["id"])

    @staticmethod
    async def claim_unprocessed_one(
        conn: asyncpg.Connection,
    ) -> WebhookInboxRow | None:
        row = await conn.fetchrow(
            """
            SELECT id, provider, provider_event_id, payload, headers,
                   received_at, processed_at
            FROM webhook_inbox
            WHERE processed_at IS NULL
            ORDER BY received_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        )
        if row is None:
            return None
        return WebhookInboxRow(
            id=row["id"],
            provider=row["provider"],
            provider_event_id=row["provider_event_id"],
            payload=_loads_json(row["payload"]),
            headers=_loads_json(row["headers"]),
            received_at=row["received_at"],
            processed_at=row["processed_at"],
        )

    @staticmethod
    async def mark_processed(conn: asyncpg.Connection, inbox_id: UUID) -> None:
        await conn.execute(
            "UPDATE webhook_inbox SET processed_at = NOW() WHERE id = $1",
            inbox_id,
        )


# -- SchedulerStateRepo ------------------------------------------------------


class SchedulerStateRepo:
    @staticmethod
    async def get_last_dispatch_at(
        conn: asyncpg.Connection,
        campaign_id: UUID,
    ) -> datetime | None:
        value = await conn.fetchval(
            """
            SELECT last_dispatch_at
            FROM scheduler_campaign_state
            WHERE campaign_id = $1
            """,
            campaign_id,
        )
        return cast("datetime | None", value)

    @staticmethod
    async def update_last_dispatch_at(
        conn: asyncpg.Connection,
        campaign_id: UUID,
        ts: datetime,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO scheduler_campaign_state (campaign_id, last_dispatch_at)
            VALUES ($1, $2)
            ON CONFLICT (campaign_id)
            DO UPDATE SET last_dispatch_at = EXCLUDED.last_dispatch_at
            """,
            campaign_id,
            ts,
        )
