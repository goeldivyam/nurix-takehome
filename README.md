# Nurix Voice Campaign Microservice

A local-only outbound-call campaign microservice built against a single
Postgres and a mock telephony provider. Campaigns group phone numbers
and dial them under a per-campaign concurrency cap, a weekly business-hour
schedule, and a configurable retry policy; failed retries beat new calls at
the system level so no campaign starves another during a retry storm. The
scheduler, state machine, webhook processor, and reclaim sweep all run
inside one FastAPI process and coordinate through Postgres advisory
semantics (`SELECT … FOR UPDATE SKIP LOCKED`, CAS on `(status,
attempt_epoch)`, the `webhook_inbox` table). The audit log is the
visualization: every scheduler decision, every state transition, and every
webhook outcome lands in `scheduler_audit` and is rendered live in a
two-tab HTML view at `/ui`.

---

## Setup

```bash
# 1. Virtual env + deps (Python 3.11+)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Config
cp .env.example .env

# 3. Bring the stack up (Postgres + app, healthcheck gated)
make up

# 4. Sanity check
curl -s localhost:8001/health
# -> {"status":"ok","pools":{"api":{...},"scheduler":{...},"webhook":{...}}}

# 5. Open the operator UI
open http://localhost:8001/ui
```

Host ports are `8001` (app) and `5442` (Postgres) to avoid colliding with
other local services; set `HOST_APP_PORT` / `HOST_PG_PORT` to remap. Inside
the container the app still listens on `8000`.

Common commands:

```bash
make up           # build + start postgres + app, wait for /health
make down         # stop + remove
make logs         # tail app logs
make reset-db     # DROP SCHEMA public + re-apply schema.sql against the live DB
make test         # pytest (unit + integration + e2e) in the host venv
make lint         # ruff + ruff-format --check + mypy app/
make format       # ruff format + ruff --fix
make demo-fairness
make demo-reclaim
make demo-reset
```

---

## Demo in 10 minutes

Three `make demo-*` targets drive the three behaviors rubric reviewers are
most likely to probe.

1. **`make demo-reset`** — wipes campaigns / calls / scheduler state / the
   webhook inbox / the audit log so a fresh demo starts from a clean
   chronology. Run this before either of the other two.

2. **`make demo-fairness`** — seeds two campaigns with different
   concurrency caps and a non-zero failure rate, then prints three
   pre-filtered `/ui` URLs, each with a one-line narrative:
   - `/ui/#audit?event_type=DISPATCH` — per-campaign DISPATCH counts
     track the `max_concurrent` ratio (A=3, B=2), not the phone-count
     ratio. Each campaign saturates to its cap in parallel.
   - `/ui/#audit?campaign_id=<A>&event_type=RETRY_DUE,DISPATCH` — each
     `RETRY_DUE` is followed within the next tick by a `DISPATCH` on the
     same call id. Retries beat new calls at the system level.
   - `/ui/#audit?campaign_id=<A>&event_type=CLAIMED,TRANSITION` — each
     `CLAIMED` row follows the nearest prior terminal `TRANSITION` on the
     same campaign within ~1s (safety-net + wake-notify). Every `CLAIMED`
     row's `extra.in_flight_at_claim` is `≤ max_concurrent − 1`.

3. **`make demo-reclaim`** — seeds one campaign, polls `GET /calls/{id}`
   until the call reaches `in_progress` (the external mapping for
   internal `DIALING`), then calls `/debug/age-dialing/{id}?by_seconds=900`
   to rewind `updated_at`. With `DEMO_MODE=true` the reclaim sweep runs
   every 5s, so a `RECLAIM_EXECUTED` (or `RECLAIM_SKIPPED_TERMINAL` if a
   webhook beats the sweep) audit row lands within ~10s of aging. The
   script narrates the wait and prints the filtered `/ui` URL.

---

## Example API usage

The API is documented at `http://localhost:8001/docs` (OpenAPI). Minimal
curl examples:

```bash
# Create a campaign. All phones must be E.164 with +cc — Nurix runs
# India and US campaigns so the validator rejects bare 10-digit numbers
# outright rather than guessing a default region.
curl -s -X POST http://localhost:8001/campaigns \
  -H 'content-type: application/json' \
  -d '{
    "name": "nyc-morning",
    "timezone": "America/New_York",
    "schedule": {
      "mon": [{"start":"09:00","end":"17:00"}],
      "tue": [{"start":"09:00","end":"17:00"}],
      "wed": [{"start":"09:00","end":"17:00"}],
      "thu": [{"start":"09:00","end":"17:00"}],
      "fri": [{"start":"09:00","end":"17:00"}],
      "sat": [], "sun": []
    },
    "max_concurrent": 5,
    "retry_config": {"max_attempts": 2, "backoff_base_seconds": 30},
    "phones": ["+14155550001","+919876543210"]
  }'

# List campaigns (cursor-paginated, api_pool).
curl -s 'http://localhost:8001/campaigns?limit=20'

# Per-campaign aggregate stats — the shape the assignment specifies.
curl -s http://localhost:8001/campaigns/<id>/stats
# -> {"total":2,"completed":1,"failed":0,"retries_attempted":1,"in_progress":1}

# Call-level status (external mapping: in_progress | completed | failed).
curl -s http://localhost:8001/calls/<id>

# Audit log — the visualization. Filters AND-compose; event_type is OR
# within a comma-separated list.
curl -s 'http://localhost:8001/audit?event_type=CLAIMED,DISPATCH&limit=50'

# Simulate an inbound provider webhook (mock).
curl -s -X POST http://localhost:8001/webhooks/provider \
  -H 'content-type: application/json' \
  -d '{"provider_event_id":"e-1","provider_call_id":"mock-xxx","status":"COMPLETED"}'
```

---

## System design

### Six-layer architecture

| Layer | Responsibility |
|---|---|
| `app/api` | FastAPI routers + Pydantic request/response schemas. No business logic. |
| `app/persistence` | Three asyncpg pools (`api` / `scheduler` / `webhook`) + repositories. Only SQL. |
| `app/scheduler` | Tick pipeline, wake signal, stuck-call reclaim, webhook processor. |
| `app/state` | Sole mutator of `calls` and `campaigns`. CAS transitions + same-txn audit. |
| `app/provider` | `TelephonyProvider` Protocol + `MockProvider` in-process adapter. |
| `app/audit` | Emitter (writes on the caller's connection) + paginated reader. |

Shared types live at the layer that owns them: `CallStatus` and
`CampaignStatus` in `app/state/types.py`, `AuditEvent` in
`app/audit/events.py`, `CallHandle` / `ProviderEvent` /
`ProviderRejected` / `ProviderUnavailable` in `app/provider/types.py`.

### Scheduler pipeline

Every wake runs one tick. One dispatch per tick keeps the cursor math
simple and the audit trail one-to-one with decisions.

1. **Eligibility** — `CampaignRepo.list_eligible_for_tick` returns every
   campaign in status `{PENDING, ACTIVE}` joined with
   `scheduler_campaign_state.last_dispatch_at` so the RR cursor is
   available without a second read.
2. **Business-hour gate** — `is_in_window` converts UTC into the
   campaign's `ZoneInfo`, picks today's local weekday, and accepts if
   the current time falls inside any `[start, end)` window.
3. **Concurrency gate** — ONE `in_flight_counts_by_campaign` GROUP BY
   call returns `{campaign_id: count}`. Campaigns where `count >=
   max_concurrent` are dropped. No N+1 as the fleet grows.
4. **Retry sweep** — among survivors, intersect with
   `find_retry_due_campaign_ids`; the oldest-last-dispatch wins the RR
   cursor tiebreak. If a retry wins, transition its oldest
   `RETRY_PENDING`-due row back to `QUEUED` via `state.transition`
   (emits a `RETRY_DUE` audit) so the next step's claim primitive
   picks it up.
5. **Round-robin pick** — if no retry won, pick the campaign with the
   oldest `last_dispatch_at` (a null cursor wins over any concrete value,
   so a brand-new campaign doesn't wait).

The picked campaign runs through **three-phase dispatch** — the pattern
that lets the scheduler scale horizontally without holding DB
connections across provider latency:

- **Phase 1** (scheduler_pool txn): snapshot pre-claim counts, run the
  SKIP LOCKED claim primitive, emit the `CLAIMED` audit row on the same
  connection. If the claim returns `None`, the row drained between
  eligibility read and claim — silent no-op.
- **Phase 2** (no DB txn): `provider.place_call(idempotency_key, phone)`
  where `idempotency_key = f"{call_id}:{attempt_epoch}"`. Catches
  `ProviderRejected` and `ProviderUnavailable`.
- **Phase 3** (scheduler_pool txn): apply the outcome via
  `state.transition`. OK → DISPATCH audit + `provider_call_id` recorded.
  REJECTED → FAILED. UNAVAILABLE → RETRY_PENDING if budget remains, else
  FAILED-exhausted. `scheduler_campaign_state.last_dispatch_at` updates
  here so the cursor is persisted across process restarts.

### State model

**Call** — `QUEUED → DIALING → IN_PROGRESS → COMPLETED | FAILED`.
Failures with retries remaining go `DIALING → RETRY_PENDING` and return
to `QUEUED` when the backoff elapses. `NO_ANSWER` / `BUSY` are
retryable. `attempt_epoch` increments at two well-defined sites — the
claim primitive (QUEUED → DIALING) and the stuck-reclaim branch
(DIALING → QUEUED) — producing a distinct idempotency key per
provider-facing dial attempt.

**Campaign** — `PENDING → ACTIVE → COMPLETED | FAILED`. The first
`QUEUED → DIALING` transition on any of a campaign's calls promotes
the campaign `PENDING → ACTIVE` atomically in the same transaction.
Every terminal call transition runs a rollup in the same transaction:
if the campaign has no calls in `{QUEUED, DIALING, IN_PROGRESS,
RETRY_PENDING}`, CAS the campaign to `COMPLETED` (any success) or
`FAILED` (all terminal calls failed). The CAS on `status='ACTIVE'`
serializes the last-two-terminal race — only one caller wins and
emits the `CAMPAIGN_COMPLETED` audit row.

### Crash safety

- **Idempotency key** at the provider port is always
  `f"{call_id}:{attempt_epoch}"`. A retry on the same attempt returns
  the same provider handle; a re-dial after epoch bump gets a fresh key.
- **Phone-level in-flight guard** — `UNIQUE(phone) WHERE status IN
  ('QUEUED','DIALING','IN_PROGRESS')`. A second campaign can't re-dial
  a number that's already in flight; `COMPLETED` / `FAILED` rows don't
  count so history is preserved.
- **Stuck-call reclaim** runs on a separate timer
  (`reclaim_sweep_interval_seconds`, independent of the tick's safety
  net). For every `DIALING` row older than
  `max_call_duration + 30s`: null `provider_call_id` → reclaim branch
  (epoch bump) directly; otherwise `provider.get_status()` with a hard
  timeout — terminal result applies at the same epoch (no bump), unknown
  or timeout bumps the epoch and requeues. Each per-row task wraps
  `try/except BaseException` so one slow `get_status` never
  head-of-line-blocks the sweep.
- **Webhook ingest** — `/webhooks/provider` runs (1) verify signature,
  (2) INSERT into `webhook_inbox` inside a transaction, (3) AFTER commit
  spawn `process_pending_inbox` as a tracked task. Spawning before
  commit would race — the processor could dequeue before the row is
  visible. A periodic safety-net loop picks up anything orphaned by a
  crash between commit and task spawn. The
  `UNIQUE(provider, provider_event_id)` index + ON CONFLICT makes
  duplicate deliveries idempotent.
- **Audit atomicity** — every state transition and its audit row are
  written on the same connection inside the same transaction. Readers
  never observe a transition without its reason.
- **CLAIMED + DISPATCH pair** — the three-phase dispatch separates the
  DB claim from the provider call. The CLAIMED audit closes the "every
  transition emits one audit row in the same txn" invariant at Phase 1;
  the DISPATCH audit at Phase 3 records the outcome. Every dispatched
  call has exactly one CLAIMED and one DISPATCH paired by
  `(call_id, attempt_epoch)`.

### Audit log as visualization

The plan's rubric point 7 ("audit log IS the visualization") is realized
via `GET /audit` + the `/ui` two-tab bundle (see `frontend/`). Every
scheduler decision, skip reason, webhook outcome, and transition lands
in `scheduler_audit` as a structured row with typed `extra`. The UI
renders them in a dense, filterable, cursor-paginated table so an
operator can walk the causal chain of any campaign from creation to
terminal rollup in under a minute.

---

## Tech choices

- **Postgres only** — `SELECT … FOR UPDATE SKIP LOCKED` gives reliable
  per-row queue semantics without a separate broker. No Celery, no
  RabbitMQ, no Redis. One fewer moving part at take-home scope and a
  documented horizontal-scale path (one Postgres, N app replicas).
- **Single-process FastAPI** — API + scheduler tick + reclaim sweep +
  webhook processor share one event loop. Cross-process coordination
  deferred to the "add a second replica" future work; the advisory-lock
  hook is already designed (see Scalability below).
- **asyncpg** (not psycopg2) — required for async non-blocking DB I/O.
- **Three asyncpg pools** — `api` / `scheduler` / `webhook`, so a
  webhook burst or a long `/audit` scan can't starve the scheduler tick.
  `/audit` reads go to `api_pool`, never `scheduler_pool`. Documented
  exception: `POST /campaigns` writes the campaign row + its seed calls
  on `scheduler_pool` because the batch enters the state machine's
  space — calls are governed by `state.transition`, and the initial
  insert populates that state space.
- **Mock provider via in-process callback**, not HTTP loopback — the
  mock invokes `handle_webhook_ingest(deps, payload, raw_body=b"",
  headers={})` directly via an `event_sink` closure wired at lifespan
  startup. No loopback port, no synthetic HMAC, unit tests stay
  hermetic. Real adapters (Twilio / Retell / Vapi) would POST HTTP to
  the same helper in production.
- **No global CPS throttle** — the assignment specifies "maximum
  concurrent calls … within a campaign." A global token bucket would
  be spec-creep. Flagged as future work for providers that enforce a
  global account rate.
- **No weights between campaigns** — the assignment lists per-campaign
  `max_concurrent`, per-campaign retry config, and per-campaign
  business hours, but nothing about inter-campaign priority. "Fairness"
  in the spec means retries-before-new inside the queue, which is the
  scheduler's retry sweep. Adding a weight field is a one-line
  extension; documented as future work.
- **No pause/resume, no cancel** — not in the assignment. The
  `TelephonyProvider` Protocol leaves `cancel(call_id)` off deliberately
  because Twilio / Retell / Vapi diverge on cancel semantics; adding it
  when a real adapter lands beats guessing its shape today.
- **Phone normalization — +cc required** — Nurix operates India and US
  campaigns in the same service. A bare 10-digit number is ambiguous
  between the two dial plans, so the Pydantic validator rejects it
  rather than guessing a default region. Each phone must carry its
  country code (+1…, +91…) so the partial unique index on `(phone)` is
  unambiguous.

### Scope boundary — voice-AI stack

This service is a **campaign orchestrator**. Its responsibility ends at
the telephony boundary. The surfaces around it are explicit ports, not
features we build here:

- **`TelephonyProvider`** (the port in `app/provider/base.py`) is a
  call-placement and call-status contract: `place_call`, `get_status`,
  `aclose`. Swapping the mock for a real adapter (Twilio / Retell /
  Vapi) is a one-file change with no scheduler / state edits.
- **Conversation engine** (TTS / STT / LLM, barge-in, audio pipeline)
  is out of scope — it sits behind its own `ConversationEngine` port
  invoked by the telephony provider on media events, not by our
  scheduler. The campaign layer passes a `script_ref` down through
  `place_call` and the engine resolves the audio loop independently.
  This matches how Retell and Vapi decompose the stack: telephony is
  I/O-bound, conversation is GPU-bound; they scale and version
  separately.
- **Per-country routing**, **AMD sub-codes**, **global CPS throttle**,
  **pause/resume** — all deferred. The abstractions support them; the
  assignment's scope does not require them. See Future Work.

---

## Fault tolerance

- **Webhook ordering is not guaranteed** — the state machine's CAS on
  `(status, attempt_epoch)` silently no-ops stale or out-of-order events
  and writes a `WEBHOOK_IGNORED_STALE` audit row with the
  expected / actual state for forensic traceability. Terminal state
  always wins; intermediate audit coverage is best-effort.
- **Reclaim confirm-then-CAS** — the sweep always calls
  `provider.get_status` before bumping the epoch. Terminal provider
  results apply at the same epoch (no bump); only unknown / timeout
  reclaims. The grace window (`max_call_duration + 30s`) is wider than
  any plausible provider status-cache TTL.
- **Idempotency key = `f"{call_id}:{attempt_epoch}"`** — increments
  only at the two sites that actually dial (claim primitive and reclaim
  branch). Retry requeue (`RETRY_PENDING → QUEUED`) does not bump; the
  next claim handles it. Terminal transitions do not bump.
- **Commit-then-spawn webhook ordering** — inbox INSERT commits before
  the processor task spawns. The `UNIQUE(provider, provider_event_id)`
  index makes duplicate deliveries idempotent. A periodic safety-net
  sweep picks up anything orphaned between commit and spawn (say a crash
  mid-request).
- **Business-hour close with in-flight calls** — the gate only blocks
  NEW dispatches. Calls already in `DIALING` / `IN_PROGRESS` drain
  naturally to terminal; the scheduler doesn't touch them. Asserted by
  the integration test
  `test_business_hour_close_with_in_flight_does_not_dispatch_new`.

---

## Scalability

- **Horizontal replicas** — run N app containers against the same
  Postgres. The claim primitive already uses `FOR UPDATE SKIP LOCKED`
  so different ticks take different rows without serializing. The
  count-then-claim concurrency gate becomes racy under multi-replica,
  so the gate + claim should be wrapped in
  `pg_try_advisory_xact_lock(hashtextextended(campaign_id::text, 0))`
  — the 64-bit hash, not the 32-bit `hashtext`, so unrelated campaigns
  don't collide in the 64-bit advisory-lock key space. Documented as
  future work rather than built; single-process correctness is
  established first.
- **Pool separation** — the three-pool split (api / scheduler /
  webhook) is already in place. Sizing is env-driven so an operator
  can tune each role against its real traffic shape.
- **Cursor-based pagination** — `GET /audit` and `GET /campaigns` use
  `(ts, id) < cursor` so new rows arriving mid-pagination never
  displace earlier pages. Shareable URLs carry the cursor so "view as
  of this page" works.
- **Webhook-inbox retention** — `webhook_inbox` is append-only and
  needs a retention cleanup to stay bounded. The retention horizon is
  `WEBHOOK_INBOX_RETENTION_DAYS` (default 7); the cleanup job itself is
  flagged as future work — ops can run the delete manually until then.

---

## Future work

- **`TelephonyProvider.cancel(call_id)`** — for campaign abort.
  Adapters diverge on semantics (Twilio "canceled" vs Retell "aborted"),
  so the Protocol deliberately omits it until a real adapter lands.
- **Per-country provider routing** — Nurix uses different providers
  for India (+91) vs the US (+1). The abstraction is ready: the
  `TelephonyProvider` Protocol is narrow, `deps.provider` is a single
  instance today, and the lifespan is the one place to wire a
  phone-prefix-routing dispatcher. Not built here because the
  assignment scope is one mock.
- **Promoting `parse_event` / `verify_signature` onto the Protocol** —
  deferred until a second adapter lands so the shape can be grounded
  rather than guessed.
- **AMD (answering-machine detection) sub-codes** — enrich
  `CallStatus` with `AMD_MACHINE`, `AMD_HUMAN` terminals once a
  provider surfaces them.
- **Weights between campaigns** — extend the RR cursor tiebreak into
  a weighted-RR under a new `weight` field on `campaigns`. Not specced
  by the assignment.
- **Global CPS throttle** — a token bucket at the provider-adapter
  layer for accounts that enforce a global rate.
- **Pause / resume** — pausable campaigns with operator control.
- **`webhook_inbox` retention cleanup task** — a daily job that
  deletes / archives rows older than `WEBHOOK_INBOX_RETENTION_DAYS`.
- **Per-campaign `max_call_duration` override** — currently global;
  some providers or use-cases need a per-campaign ceiling.

---

## Testing

```bash
make test        # pytest: unit + integration + e2e
make lint        # ruff + mypy
```

The suite covers every scheduler invariant in the rubric —
concurrency-gate starvation, retry-before-new at the system level,
multi-retry RR fairness, continuous channel reuse via `wake.notify`,
business-hour close with in-flight calls, the CLAIMED+DISPATCH
(call_id, attempt_epoch) pair, PENDING→ACTIVE promotion under forced
rollback, the last-two-terminal race producing exactly one
`CAMPAIGN_COMPLETED` audit row, the reclaim null-handle short-circuit,
and the retroactive-stale-event invariant. Integration tests run
against testcontainers Postgres; the end-to-end test
(`tests/e2e/test_full_stack.py`) drives the running docker-compose
stack via HTTP.
