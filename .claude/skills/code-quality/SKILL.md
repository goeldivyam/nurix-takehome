---
name: code-quality
description: Naming conventions, Python code standards, domain terminology, and anti-patterns for the Nurix outbound voice campaign microservice
---

# Code Quality

## Naming

Names reveal intent. Self-documenting beats commenting.

```python
# GOOD
claim_next_call()
emit_scheduler_audit()
is_in_business_hours()
max_concurrent_reached()
rebuild_idempotency_key()

# BAD
get_call()
log()
check_time()
at_max()
build_key()
```

Rules:
- Booleans: `is_` / `has_` / `should_` / `can_`
- Collections: plural (`campaigns`, `calls`, `windows`)
- Consistent verbs: pick one and stick (`fetch_` everywhere, not `get_` / `load_` / `read_` mixed)
- No abbreviations except industry-standard (`CPS`, `DRR`, `WRR`, `SIP`, `DTMF`)

## Domain language

Use project vocabulary, not generic framework terms:

| Use | Not |
|---|---|
| `campaign` | `group`, `batch`, `job` |
| `call` | `task`, `request`, `item` |
| `dispatch` | `send`, `emit`, `trigger` |
| `attempt_epoch` | `version`, `generation` |
| `next_attempt_at` | `retry_time`, `scheduled_for` |
| `max_concurrent` | `pool_size`, `thread_limit` |
| `business_hours` | `schedule_window` |
| `provider_event_id` | `external_id`, `webhook_id` |
| `idempotency_key` | `dedup_key`, `unique_id` |

The idempotency key is always `f"{call_id}:{attempt_epoch}"`. Never plain `call_id`.

## Python specifics

- **Type hints on every public function.** No bare `def foo(x, y)` in non-test code.
- **No mutable default args.** `def f(x: list = [])` is a bug waiting; use `x: list | None = None` + `x = x or []`.
- **Context managers for every resource.** `async with pool.acquire() as conn:` — never manual `pool.acquire()` + `await conn.close()`.
- **`Protocol` for port definitions** (telephony provider, clocks). Reach for `ABC` only if inheritance is needed.
- **No `print()` in non-test code** — use `logging`.
- **No bare `except:` or `except Exception:`** without re-raise. Catch specific types, log, rethrow if not handled.
- **Avoid broad decorators.** If `@retry` wraps business logic, the retry policy belongs in the business layer, not a decorator.

## Enums that map to DB values

- The value of every enum member used for persistence MUST equal its DB string literal. No translation layer. Example: `CallStatus.QUEUED.value == "QUEUED"`.
- SQL references the enum via `.value`, never a bare string literal:
  ```python
  # GOOD
  await conn.execute("UPDATE calls SET status = $1 WHERE id = $2", CallStatus.DIALING.value, call_id)
  # BAD — drifts silently if the enum is renamed
  await conn.execute("UPDATE calls SET status = 'DIALING' WHERE id = $1", call_id)
  ```
- `CallStatus` (closed set: `DIALING, IN_PROGRESS, COMPLETED, FAILED, NO_ANSWER, BUSY`) lives in `app/state/`. Provider adapters translate their native vocabulary into this set; never the other way around.

## Audit emission signature

- Audit writes go through one function: `emit_audit(conn: Connection, event: AuditEvent) -> None`. `conn` is the first positional argument. **Never defaulted. Never fetched from a pool inside `emit_audit`.** Callers pass their own transaction's connection so the audit row commits atomically with the transition.
- `AuditEvent` is a pure frozen dataclass. No `.save()`, `.emit()`, or any method that performs I/O.

## Pydantic vs dataclass boundary

- **Pydantic** models live in `app/api/schemas.py` ONLY, at the HTTP boundary (request parse + response serialize).
- **Frozen `@dataclass(frozen=True, slots=True)`** for every internal value object: `AuditEvent`, `CallHandle`, scheduler decisions, state-transition records.
- Internal code never imports pydantic. Crossing the boundary happens explicitly in `app/api/` (`Schema.model_validate(...)` → internal dataclass, and the reverse on response).

## Async discipline

- `async def` means **no blocking calls inside**. No `time.sleep`, no `requests.get`, no sync DB drivers.
- CPU work or sync library needed? Wrap with `await asyncio.to_thread(...)`.
- **Do not share one asyncpg connection across coroutines.** `asyncio.gather` tasks each need their own `pool.acquire()`.
- Don't fire-and-forget `asyncio.create_task(...)` without capturing the task and handling its exception — orphaned tasks silently swallow errors.
- **Prefer `async with asyncio.TaskGroup():` over `asyncio.gather(...)`** for structured concurrency (Python 3.11+). TaskGroup cancels siblings on any failure and propagates exceptions cleanly. Reserve tracked `asyncio.create_task(...)` for fire-and-forget daemons (scheduler tick loop, webhook processor, stuck-reclaim sweep — see `backend-conventions` for the tracked-set pattern).

## No dead code, no half-built

- Remove unused imports, variables, functions opportunistically when you touch a file.
- No `# TODO` in submitted code — either do it or delete the thought.
- No commented-out code blocks.
- No backwards-compatibility shims for code that doesn't exist yet.
- If logic is repeated in 2+ places, extract a helper. Now, not later.

## No hardcoding

- All defaults in `config.py` + env vars. No magic numbers in business logic.
- Named constants for tunables: `MAX_CONCURRENT_DEFAULT = 5`, `STUCK_RECLAIM_SECONDS = 600`, `MAX_RETRIES_DEFAULT = 3`, `RETRY_BACKOFF_BASE_SECONDS = 2`.

## Comments

- Default: write none. Good names make comments redundant.
- Only comment when the **why** is non-obvious — a subtle invariant, an intentional race, a workaround for a specific behavior.
- Never comment the **what** (the code says what).
- Docstrings: only on public API of a module. One sentence is usually enough.

## Time and timezones

- All timestamps: `TIMESTAMPTZ` in Postgres, `datetime` with `tzinfo` in Python.
- Business-hour check: convert UTC "now" to the campaign's timezone **once**, compare against that campaign's schedule.
- Never store naive timestamps. Never compare across timezones without explicit conversion.
- `zoneinfo` (stdlib) is the canonical timezone source; avoid `pytz`.
- **Wall clock vs monotonic**: use `datetime.now(UTC)` for stored / scheduled times (business-hour windows, `next_attempt_at`, audit timestamps). Use `time.monotonic()` for **duration measurements** — tick-loop sleeps, reclaim-window checks, backoff delays. NTP skew would otherwise corrupt the `max_call_duration + 30s` reclaim window and retry backoffs.

## Logging

- Structured logs (JSON or key=value) on stdout. docker-compose captures them.
- `logger = logging.getLogger(__name__)` at module scope.
- Log at dispatch decisions, state transitions, webhook receipt, recovery actions — one line each. Don't spam tick-level logs.
