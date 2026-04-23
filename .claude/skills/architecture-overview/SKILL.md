---
name: architecture-overview
description: File-level architecture map, tech stack, module layout, and database tables for the Nurix outbound voice campaign microservice. Update as modules land.
---

# Architecture Overview

> The conceptual model (subsystem layers, policy, non-negotiables) lives in `CLAUDE.md`. This file is the file-level / table-level map: what code lives where, what tables exist, what's public. Read both.

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| API framework | FastAPI + uvicorn |
| Database | Postgres 16 (docker) |
| DB driver | asyncpg |
| Queue semantics | `SELECT ... FOR UPDATE SKIP LOCKED` over Postgres — no external broker |
| Deployment | docker-compose (local only) |
| Testing | pytest + pytest-asyncio |
| Lint / type | ruff + mypy |

## Module layout

```
app/
  api/              # FastAPI routers, pydantic request/response schemas
  persistence/      # asyncpg pools + repositories (CampaignRepo, CallRepo, AuditRepo, WebhookInboxRepo)
  scheduler/        # tick loop, round-robin picker, eligibility filters, retry sweep, SKIP LOCKED claim
  provider/         # TelephonyProvider Protocol + MockProvider impl
  state/            # campaign + call state machines, transition functions
  audit/            # event emitter + query
  config.py         # env-driven defaults
  main.py           # uvicorn entry; starts scheduler loop as a background task
schema.sql          # single source of truth for DB schema
docker-compose.yml  # postgres + app service
tests/
  unit/             # state machine, scheduler policy, provider mock
  integration/      # full path via API against docker-compose Postgres
README.md
CLAUDE.md
```

## Key tables

Fill in concrete columns as `schema.sql` is written.

- `campaigns` — id, name, status, timezone, schedule (jsonb weekly calendar), max_concurrent, retry_config (jsonb), created_at, updated_at
- `calls` — id, campaign_id, phone, status, attempt_epoch, retries_remaining, next_attempt_at, provider_call_id, created_at, updated_at + partial unique index on `(phone) WHERE status IN ('QUEUED','DIALING','IN_PROGRESS')`
- `scheduler_campaign_state` — campaign_id (PK), last_dispatch_at — scheduler-owned, API reads only
- `webhook_inbox` — id, provider_event_id UNIQUE, payload jsonb, received_at, processed_at NULL
  - Append-only by the ingest route; rows are NOT deleted on processing. A daily cleanup job archives or deletes rows older than `WEBHOOK_INBOX_RETENTION_DAYS` (default 7) to bound growth. For the initial build the cleanup task is README-documented as future work; ops can run the delete manually until then.
- `scheduler_audit` — id, ts, event_type, campaign_id, call_id, reason (text), state_before, state_after, extra (jsonb)
  - **Event types**: `DISPATCH`, `RETRY_DUE`, `SKIP_BUSINESS_HOUR`, `SKIP_CONCURRENCY`, `WEBHOOK_RECEIVED`, `WEBHOOK_IGNORED_STALE` (CAS no-op because state/epoch mismatched; row written in the same transaction as the inbox insert so operators see the "why it didn't move" without inferring from silence), `TRANSITION`, `RECLAIM_SKIPPED_TERMINAL` (provider `get_status` returned terminal — outcome applied on same `attempt_epoch`), `RECLAIM_EXECUTED` (`get_status` returned unknown — row reset to `QUEUED` with bumped epoch), `CAMPAIGN_COMPLETED`.
  - `DISPATCH` events carry a decision snapshot in `extra`: `{in_flight_before, max_concurrent, retries_pending_system, rr_cursor_before}`. Lifts "why this call, why now" from operator-inference to explicit fact.
  - Every audit row is written on the caller's connection inside the same transaction as its triggering state transition. See `backend-conventions` skill for the invariant.

## Shared type ownership

- **`CallStatus` enum** (closed: `{DIALING, IN_PROGRESS, COMPLETED, FAILED, NO_ANSWER, BUSY}`) lives in `app/state/`. Provider / audit / api import it. Provider adapters translate vendor-native status vocabulary into this closed set. `app/provider/` importing from `app/state/` is a **type-only import** of a closed enum — not a behavior dependency; provider never calls into state.
- **`AuditEvent` dataclass** lives in `app/audit/` as a pure frozen dataclass (no `.save()` / `.emit()` — no I/O methods). Writes go through `emit_audit(conn, event)`; see `code-quality` and `backend-conventions` skills.
- **`SchedulerWake` port** lives in `app/scheduler/`. Methods: `notify() -> None` and `async wait(timeout: float | None) -> bool`. Implementation = `asyncio.Event`. Dependency-injected into state + webhook processor (which call `notify()` on every terminal transition / inbox dequeue). Scheduler loop awaits `wait()` between ticks.
- **`CallHandle` dataclass** lives in `app/provider/` — `{ provider_call_id: str, accepted_at: datetime }`. Provider exceptions (`ProviderRejected(reason_code)`, `ProviderUnavailable`) also owned here — scheduler / state catch these types, never vendor-specific errors.
- **`parse_event(payload) -> ProviderEvent`** is an adapter-module-level function (mock: `app/provider/mock.py::parse_event`). Invoked by the webhook processor after `webhook_inbox` dequeue. NOT on the `TelephonyProvider` Protocol — promoting it there is deferred until a second adapter lands.

## Public API surface (target)

- `POST /campaigns` — create with phones, timezone, schedule, retry config, max_concurrent
- `GET /campaigns` — list
- `GET /campaigns/{id}` — detail
- `GET /campaigns/{id}/stats` — total, completed, failed, retries_attempted (matches assignment spec)
- `GET /calls/{id}` — single call status (maps internal states to `in_progress | completed | failed` per assignment)
- `POST /webhooks/provider` — ack-then-process
- `GET /audit` — filterable scheduler decisions (observability surface). Cursor-based pagination using the composite `(ts, id)` — cursor-not-offset so late arrivals in the same `ts` bucket can't cause missed rows on the next page. Default page size 100, max 500. Filters: `campaign_id`, `event_type`, `from_ts`, `to_ts`, free-text `reason_contains`.

## Scheduler loop (one process, one event loop)

```
while running:
    await tick()  # single dispatch decision per tick
    await asyncio.sleep(idle_interval if nothing dispatched else 0)
```

Woken by:
- Call completion / failure / no-answer (state machine emits signal)
- Webhook inbox processing (after a state transition)
- Periodic timer (safety net, e.g. every 1s)

## Update pattern

When a new module, route, or table lands, update the relevant section here. Paired with the `update-docs` skill.
