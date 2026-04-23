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
- `scheduler_audit` — id, ts, event_type, campaign_id, call_id, reason (text), state_before, state_after, extra (jsonb)
  - `DISPATCH` events carry a decision snapshot in `extra`: `{in_flight_before, max_concurrent, retries_pending_system, rr_cursor_before}`. Lifts "why this call, why now" from operator-inference to explicit fact, with no new event type.
  - Every audit row is written on the caller's connection inside the same transaction as its triggering state transition. See `backend-conventions` skill for the invariant.

## Shared type ownership

- **`CallStatus` enum** (closed: `{DIALING, IN_PROGRESS, COMPLETED, FAILED, NO_ANSWER, BUSY}`) lives in `app/state/`. Provider / audit / api import it. Provider adapters translate vendor-native status vocabulary into this closed set.
- **`AuditEvent` dataclass** lives in `app/audit/`. State / scheduler import it.

## Public API surface (target)

- `POST /campaigns` — create with phones, timezone, schedule, retry config, max_concurrent
- `GET /campaigns` — list
- `GET /campaigns/{id}` — detail
- `GET /campaigns/{id}/stats` — total, completed, failed, retries_attempted (matches assignment spec)
- `GET /calls/{id}` — single call status (maps internal states to `in_progress | completed | failed` per assignment)
- `POST /webhooks/provider` — ack-then-process
- `GET /audit` — filterable scheduler decisions (observability surface)

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
