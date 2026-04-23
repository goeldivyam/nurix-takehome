from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import asyncpg

MAX_LIMIT = 500
DEFAULT_LIMIT = 100


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


def encode_cursor(ts: datetime, audit_id: int) -> str:
    payload = json.dumps({"ts": ts.isoformat(), "id": audit_id}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
    obj = json.loads(raw.decode("utf-8"))
    return datetime.fromisoformat(obj["ts"]), int(obj["id"])


def _loads_extra(value: Any) -> dict[str, Any]:
    # asyncpg returns JSONB as str when no codec is registered, dict otherwise.
    # Keep the codec story uniform across test / prod — decode here.
    if value is None:
        return {}
    if isinstance(value, str):
        return cast(dict[str, Any], json.loads(value))
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    raise TypeError(f"unexpected JSONB value type: {type(value)!r}")


async def query_audit(
    api_pool: asyncpg.Pool[Any],
    *,
    campaign_id: UUID | None = None,
    event_type: str | Sequence[str] | None = None,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
    reason_contains: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[AuditRow], str | None]:
    if limit <= 0 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be in (0, {MAX_LIMIT}]; got {limit}")

    clauses: list[str] = []
    args: list[Any] = []

    def bind(value: Any) -> str:
        args.append(value)
        return f"${len(args)}"

    if campaign_id is not None:
        clauses.append(f"campaign_id = {bind(campaign_id)}")
    if event_type is not None:
        if isinstance(event_type, str):
            clauses.append(f"event_type = {bind(event_type)}")
        else:
            clauses.append(f"event_type = ANY({bind(list(event_type))}::text[])")
    if from_ts is not None:
        clauses.append(f"ts >= {bind(from_ts)}")
    if to_ts is not None:
        clauses.append(f"ts <= {bind(to_ts)}")
    if reason_contains:
        # Escape ILIKE meta-characters so a caller searching for `100%` doesn't
        # accidentally match every row.
        escaped = reason_contains.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append(f"reason ILIKE {bind(f'%{escaped}%')}")
    if cursor is not None:
        cursor_ts, cursor_id = decode_cursor(cursor)
        clauses.append(f"(ts, id) < ({bind(cursor_ts)}, {bind(cursor_id)})")

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_placeholder = bind(limit)

    # All user-supplied values travel via $N placeholders; `where_sql` and
    # `limit_placeholder` are built from static fragments owned here.
    sql = (
        "SELECT id, ts, event_type, campaign_id, call_id, reason, "  # noqa: S608
        "state_before, state_after, extra "
        f"FROM scheduler_audit {where_sql} "
        f"ORDER BY ts DESC, id DESC LIMIT {limit_placeholder}"
    )

    async with api_pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)

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
            extra=_loads_extra(r["extra"]),
        )
        for r in rows
    ]
    next_cursor: str | None = None
    if len(result) == limit and result:
        last = result[-1]
        next_cursor = encode_cursor(last.ts, last.id)
    return result, next_cursor
