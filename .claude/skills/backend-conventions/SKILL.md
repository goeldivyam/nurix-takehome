---
name: backend-conventions
description: asyncpg pool discipline, Postgres query patterns, state machine transitions, SKIP LOCKED claim, transaction rules, and timezone handling
---

# Backend Conventions

## Python virtual environment

One venv at repo root: `.venv/`. Always activate before running local commands.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Inside docker-compose, the image handles the install at build time.

## Database schema

- **ONE file**: `schema.sql`. Single source of truth.
- **No migration files** for this project. Edit `schema.sql` directly; docker-compose recreates the DB from it for a clean local reset.
- Tables carry explicit `created_at TIMESTAMPTZ DEFAULT NOW()` and `updated_at TIMESTAMPTZ DEFAULT NOW()` where mutation history matters.

## asyncpg pools — three, not one

Separate pools by role:

| Pool | Role | Size |
|---|---|---|
| `api_pool` | API read path (`GET /campaigns`, `GET /campaigns/{id}/stats`, `GET /calls/{id}`, `GET /audit`) | 5–10 |
| `scheduler_pool` | Scheduler + state writes | 5–10 |
| `webhook_pool` | Webhook ack (fast insert + return) | 1–3 |

Rationale: a webhook burst must not starve the API or the scheduler. The `/audit` read endpoint MUST use `api_pool` — NOT `scheduler_pool` — otherwise an operator running forensic queries steals capacity from the tick loop.

## Pool acquire discipline

**Every acquire uses a context manager:**

```python
async with scheduler_pool.acquire() as conn:
    await conn.execute(...)
```

**NEVER double-acquire inside a single `async with`:**

```python
# WRONG — holds one conn, grabs a second; deadlocks at pool size
async with pool.acquire() as conn:
    helper_val = await pool.fetchval("SELECT ...")  # second acquire!
    await conn.execute("INSERT ...", helper_val)

# RIGHT — pass conn to the helper
async def helper(conn, ...):
    return await conn.fetchval("SELECT ...")

async with pool.acquire() as conn:
    helper_val = await helper(conn, ...)
    await conn.execute("INSERT ...", helper_val)
```

**Never share one connection across coroutines.** Each `asyncio.gather` task that touches DB needs its own `pool.acquire()`.

## Claim pattern — SKIP LOCKED

**Two-level selection.** The scheduler tick loop chooses (a) WHICH call to claim next via the cross-campaign policy in CLAUDE.md (system-level retry sweep across all campaigns → then round-robin across eligible campaigns), then (b) issues the **per-campaign claim primitive** below against the selected campaign. Retry priority lives in the scheduler — the primitive itself is FIFO within the campaign.

The per-campaign claim primitive — one atomic statement:

```sql
WITH candidate AS (
    SELECT id, attempt_epoch
    FROM calls
    WHERE campaign_id = $1
      AND status = 'QUEUED'
      AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
    ORDER BY created_at ASC  -- FIFO within the campaign
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE calls
SET status = 'DIALING',
    attempt_epoch = calls.attempt_epoch + 1,
    updated_at = NOW()
FROM candidate
WHERE calls.id = candidate.id
RETURNING calls.*;
```

- `SKIP LOCKED` lets multiple schedulers claim different rows without serializing.
- `FOR UPDATE` holds the row lock until UPDATE commits.
- Claim + transition are atomic — no window where a row is claimed but not yet transitioned.
- DO NOT add `ORDER BY retries_remaining DESC` here — that would encode per-campaign retry priority and contradict the system-level rule (retries-before-new applies across all campaigns, not within one). Retry priority is decided at the scheduler layer via the retry sweep step.

## State transitions — compare-and-swap

Every state change uses CAS on `(id, status, attempt_epoch)`:

```sql
UPDATE calls
SET status = $new_status, updated_at = NOW()
WHERE id = $id
  AND status = $expected_status
  AND attempt_epoch = $expected_epoch
RETURNING *;
```

- Empty `RETURNING` → transition was already applied (or epoch has moved). Caller handles idempotently.
- Every transition emits one audit row in the same transaction.

## Query discipline

- **No UI-facing query without `LIMIT` / pagination.**
- **Prefer JOIN over N+1.** If you see a loop making a query per iteration, stop and rewrite.
- **Every `WHERE` / `ORDER BY` on a large table has an index.** Verify with `EXPLAIN ANALYZE` when in doubt.
- **`SELECT *` only in claim and audit paths.** Elsewhere, enumerate columns.

## Transactions

- Explicit `async with conn.transaction():` for multi-statement writes.
- **State transition + audit row must be in the same transaction on the same connection.** If the audit fails, the transition rolls back.
- **Audit writes use the caller's connection — NEVER `audit_pool`.** Reject any `audit_pool.execute(...)` / `audit_pool.fetchval(...)` inside a write path, and any `asyncio.create_task(emit_audit(...))` after a transition. `audit_pool` is strictly for observability reads (`GET /audit`). Mixing pools here either loses audit rows on crash (breaks rubric #7 — the audit log IS the visualization) or double-acquires a pool and deadlocks.
- **Webhook ack MUST NOT be in a transaction with downstream processing.** The `/webhooks/provider` endpoint does one `INSERT INTO webhook_inbox` and returns `200`; processing is a separate background task reading the inbox.

## Timezone handling

- Postgres: always `TIMESTAMPTZ`, never `TIMESTAMP`.
- Python: always `datetime` with `tzinfo`, never naive. Use `zoneinfo` (stdlib) for campaign timezones.
- Business-hour check pattern:
  ```python
  now_in_campaign_tz = datetime.now(tz=ZoneInfo(campaign.timezone))
  day_key = now_in_campaign_tz.strftime("%a").lower()  # "mon", "tue", ...
  current_time = now_in_campaign_tz.time()
  for window in campaign.schedule[day_key]:
      if window.start <= current_time < window.end:
          return True
  return False
  ```
- Windows that cross midnight (22:00–02:00) → split into two rows (22:00–23:59 + 00:00–02:00). No wrap logic.
- **Validate `start < end` at write time.** Reject any campaign schedule row where `start >= end` (in the Pydantic request schema). Otherwise a silently-wrapping row makes the business-hour predicate return false all day and the campaign never dispatches.

## Provider abstraction

- All telephony interactions go through `app/provider/TelephonyProvider` (a `Protocol`).
- Scheduler never imports provider code; it depends on the Protocol.
- Webhook processing reads the provider's event format but doesn't *talk back* — it calls `state.transition(...)`.
- Adding a real provider later = implementing the Protocol + registering in `config.py`. No changes to scheduler or state.

## Configuration

- `app/config.py` reads env vars at startup; exposes a frozen `Settings` dataclass.
- Defaults live here, not scattered across modules: `MAX_CONCURRENT_DEFAULT`, `STUCK_RECLAIM_SECONDS`, `STUCK_RECLAIM_GET_STATUS_TIMEOUT_SECONDS`, `MAX_RETRIES_DEFAULT`, `RETRY_BACKOFF_BASE_SECONDS`, `SCHEDULER_IDLE_INTERVAL_MS`, `SCHEDULER_SAFETY_NET_SECONDS`.

## FastAPI lifespan owns pools

- All three asyncpg pools (`api_pool`, `scheduler_pool`, `webhook_pool`) are created inside the FastAPI `lifespan` async context manager and closed on shutdown. NEVER at module import.
- Tests that need pools spin them up in a fixture with the same lifespan semantics — no global singleton pool.
- The scheduler tick loop and webhook processor are started inside `lifespan` AFTER pools are ready and cancelled cleanly BEFORE pools close.

## Background tasks have lifecycle

- Every `asyncio.create_task(...)` for scheduler tick, webhook processor, or stuck-reclaim sweep is stored in a module-level `set[asyncio.Task]` and attaches a done-callback that logs unhandled exceptions and removes the task from the set.
- On shutdown: cancel all tasks, `await asyncio.gather(*tasks, return_exceptions=True)`. Orphaned tasks that silently swallow exceptions are a Critical reject.

## Pydantic vs dataclass boundary

- Pydantic models live in `app/api/schemas.py` ONLY — for HTTP request parsing and response serialization.
- Internal value objects (`AuditEvent`, `CallHandle`, scheduler decisions) are frozen `@dataclass(frozen=True, slots=True)`. Crossing the boundary happens explicitly in `app/api/`.
- Codified further in the `code-quality` skill.
