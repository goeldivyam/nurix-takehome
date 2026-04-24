# Nurix Voice Campaign Microservice

Upload a list of phone numbers, set business hours and a retry policy, and the service dials them at the right time — respecting per-campaign concurrency caps, retrying failed calls before starting new ones, and logging every decision so you can see exactly what happened.

---

## The layers

All of the following run inside **one FastAPI process** sharing **one asyncio event loop** and a **single Postgres database**.

**Frontend.** Plain HTML / JS / CSS served at `/ui`. No framework, no build step. The page is a thin view over the audit log.

**Backend API.** FastAPI routes for campaign CRUD, call status, per-campaign stats, audit reads, and webhook ingest. OpenAPI docs at `/docs`.

**Scheduler.** A background task that decides which call to dial next. Runs on the same event loop as the HTTP routes — not a separate server, not Celery. Wakes instantly when a call completes so freed concurrency slots are reused immediately ("continuous channel reuse").

**Dialer.** The `TelephonyProvider` interface — a Python port with `place_call` and `get_status`. The scheduler calls it as a regular function. In production it would be a Twilio / Vapi / Retell adapter; locally it's the mock.

**Listener + webhook processor.**
- *Listener* = `POST /webhooks/provider`, an HTTP endpoint. When the provider pushes a status update, the listener writes it to the `webhook_inbox` table and returns `200` immediately.
- *Processor* = a background task that drains the inbox, turns each event into a state transition, and wakes the scheduler.

*Spec deviation — honest note.* The assignment describes two provider APIs: trigger a call and check status (a polling model). We added the webhook path because every real provider (Twilio, Vapi, Retell) pushes updates, and push-based events support continuous channel reuse better than periodic polling. The polling API is still used — the stuck-call reclaim sweep calls `get_status` as a best-effort confirm before reclaiming. Swapping the whole path back to polling is ~50 lines: a loop over `DIALING` rows calling `get_status` and feeding the same state machine.

**State machine.** The single function allowed to change a call's status. Every caller (scheduler, webhook processor, reclaim) goes through it. Before every status change it verifies the call is still in the state it expects; if anything else has already moved the call, the change silently no-ops. This is how we tolerate out-of-order webhooks, concurrent retries, and reclaim races without corrupting state or using locks. Every successful change writes its audit row in the same transaction, so a status change is never recorded without its reason. When the last call in a campaign terminates, the state machine rolls the campaign up to `COMPLETED` or `FAILED`.
- *Call states:* `QUEUED → DIALING → IN_PROGRESS → COMPLETED | FAILED`; retryable outcomes detour through `RETRY_PENDING → QUEUED`.
- *Campaign states:* `PENDING → ACTIVE → COMPLETED | FAILED`. A campaign is `COMPLETED` if any call succeeded; `FAILED` if every call failed.

**Audit log.** Append-only table of every decision, skip, transition, and webhook outcome. **The audit log is the visualization** — the `/ui` page is a filterable, paginated view over it.

**Mock provider.** Stands in for Twilio. Implements `place_call` (accepts the dial, returns a handle) and `get_status` (used by the reclaim sweep). It plays two roles at once: the outbound adapter behind the Dialer interface, and the external provider that pushes events to the listener — a job that in production belongs to Twilio itself, not to the adapter. At dispatch it pre-rolls the outcome and fires simulated events on a timer:
- *Happy path:* `DIALING` → sleep → `IN_PROGRESS` → sleep → `COMPLETED` (full duration).
- *Failure paths:* `DIALING` → sleep → terminal (`FAILED` / `NO_ANSWER`), skipping `IN_PROGRESS`.
- *Fixed timing* — 3s per call by default, 15s in demo mode. No jitter.
- *Not simulated:* `BUSY`, provider rejections (invalid number, blocked), infrastructure timeouts. The classifier handles them if they arrive; the mock just doesn't generate them.

---

## Architecture

```
  Browser                                  Provider push
     │                             (mock simulates in-process)
     │ HTTP                                     │
     ▼                                          ▼
 ┌─ FastAPI process (one asyncio event loop) ────────────┐
 │                                                       │
 │   API routes              Webhook listener            │
 │       │                          │                    │
 │       │                          ▼                    │
 │       │                  (writes inbox row)           │
 │       │                                               │
 │       ▼                                               │
 │   State machine (sole mutator; paired audit row)      │
 │       ▲        ▲        ▲                             │
 │       │        │        │                             │
 │   Scheduler  Webhook  Reclaim                         │
 │    loop      proc.    sweep                           │
 │       │                   │                           │
 │       └─────────┬─────────┘                           │
 │                 ▼                                     │
 │         Dialer (mock provider)                        │
 │                                                       │
 └───────────────────────┬───────────────────────────────┘
                         │ asyncpg
                         ▼
                      Postgres
          (campaigns, calls, scheduler_audit,
           webhook_inbox, scheduler_campaign_state)
```

---

## Database tables

Full DDL lives in `schema.sql`.

- **`campaigns`** — campaign config (schedule, concurrency cap, retry policy) + lifecycle status. `max_concurrent` defaults to 5 when omitted on create.
- **`calls`** — one row per phone per campaign. Status, attempt number, retries remaining. A partial unique index on `(phone)` prevents the same number being dialed twice at once.
- **`scheduler_campaign_state`** — stores `last_dispatch_at` per campaign so the scheduler can rotate fairly across campaigns, and the rotation survives restarts.
- **`webhook_inbox`** — accepted provider events, keyed by `(provider, provider_event_id)` so replays are idempotent. Example row: when a call finishes, the provider pushes `{provider_event_id: "e-42", provider_call_id: "call-xyz", status: "COMPLETED"}` — the listener writes one row here, the processor reads it later.
- **`scheduler_audit`** — append-only log of every decision and transition. Backs the UI.

---

## How the scheduler works

**The wake signal.** An `asyncio.Event` held inside the FastAPI process. Whenever a call reaches a terminal status — a webhook arrives, a dial-time provider rejection lands, or the reclaim sweep resolves a stuck row — the state machine calls `event.set()`. The scheduler is parked on `await event.wait()` and resumes within microseconds. That's continuous channel reuse.

**The safety-net timer.** A ~1-second fallback that wakes the scheduler even if no event is set. Two jobs: time-triggered work that nobody fires a wake for (a retry's backoff elapsing, a business-hour window opening), and a guarantee that the scheduler still runs if a wake is ever lost.

**The loop.** Wait for either the wake signal or the safety-net timer, lower the flag, run one tick. Lowering the flag **before** the tick is the important bit: if a wake comes in while the tick is running, the flag goes back up and the next `wait()` returns immediately. If we lowered it after, that mid-tick wake would be erased — and the completion it announced would wait for the safety-net timer.

**One tick.**

1. **Eligibility.** Find campaigns in `PENDING` / `ACTIVE`, in business hours right now, with work to do.
2. **Concurrency gate.** Drop any campaign already at its `max_concurrent`. Retries and new calls both pass through this gate — a retry on a saturated campaign waits exactly like a new call.
3. **Retry sweep.** Among survivors, if any campaign has a retry whose backoff has elapsed, pick it first. **Retries beat new calls at the system level**, not per-campaign. When multiple campaigns have retries due, the same round-robin order (by `last_dispatch_at`) decides who goes first. A retry dispatch advances `last_dispatch_at` exactly like a new-call dispatch — that rotation is what keeps a retry-heavy campaign from monopolizing the slot.
4. **Round-robin pick.** Otherwise pick the campaign with the oldest `last_dispatch_at`.
5. **Dispatch.** Claim the row, call the provider, record the outcome. One dispatch per tick. Every step writes an audit row with its reasoning.

---

## Retry handling

Calls retry on outcomes that might succeed on redial; they fail hard on outcomes that won't.

- **Retryable** — `NO_ANSWER`, `BUSY`, transient provider errors. The call moves to `RETRY_PENDING` with `next_attempt_at = NOW() + base × 2^attempt ± 20% jitter`. `base` and max attempts come from the campaign's `retry_config`.
- **Terminal** — provider rejections (invalid number, blocked) or an explicit `FAILED` from the provider. The call moves straight to `FAILED`.
- **Exhausted** — when `retries_remaining` hits zero, the next retryable outcome becomes `FAILED`.

---

## Tech choices

- **Postgres only.** `SELECT … FOR UPDATE SKIP LOCKED` gives reliable per-row queue semantics without a broker.
- **Single process.** One event loop runs API + scheduler + webhook processor + reclaim sweep. Scale horizontally by running N replicas against the same Postgres.
- **asyncpg.** Async non-blocking DB I/O.
- **Three DB pools** (api / scheduler / webhook). A webhook burst or a long audit scan can't starve the scheduler tick.

---

## Fault tolerance

- **Webhook ordering is not guaranteed.** Stale or out-of-order events silently no-op via the state machine's check-before-change rule (see Layers). Once a call reaches a terminal status, later events can't move it — done is done.
- **Stuck-call reclaim.** If a call has been stuck in `DIALING` for too long (`max_call_duration + 30s`), a background sweep asks the provider `get_status` as a safety check. If the provider confirms a terminal status, we apply it; if it returns unknown or times out, we requeue the call for a fresh dial attempt.
- **Idempotency.** Every provider-facing dial carries `idempotency_key = f"{call_id}:{attempt_epoch}"`. A retry on the same attempt returns the same handle.
- **Commit-then-spawn webhook ingest.** The inbox row is committed before the webhook processor task is spawned — otherwise the processor could try to read a row that isn't visible yet. A periodic safety-net sweep picks up any row left behind if the process crashes between the two steps.

---

## Scalability

- **Horizontal replicas.** Run N app containers against one shared Postgres. All replicas see all campaigns — there's no partitioning. Each replica runs its own scheduler loop and races to claim work from the shared `calls` table; `SELECT … FOR UPDATE SKIP LOCKED` ensures no two replicas dial the same row. Work spreads naturally across replicas.
- **One gap** in the multi-replica story: the scheduler checks `max_concurrent` by counting in-flight calls, then claiming. In a single process that's race-free. In multi-replica, two ticks can count at the same moment, both decide "one slot left," and both claim — briefly exceeding the cap. Fix (future work): wrap the count + claim in a per-campaign advisory lock so only one replica counts-and-claims for a given campaign at a time.
- **Pool separation.** Each replica opens three asyncpg pools (api / scheduler / webhook) so a webhook burst or a long audit scan can't starve the scheduler. Sizes are env-driven. At high replica counts, total connections can exceed Postgres's cap — add PgBouncer in front of the DB when that happens.
- **Cursor-based pagination.** Each page is fetched by "give me the next batch after this marker (timestamp, id)" rather than "skip N rows." Two wins: new rows arriving while you paginate don't shift existing pages, and performance stays flat as the audit log grows (no `OFFSET` scan penalty).

---

## Deliberately out of scope

- **Cancel / pause / resume.** Not in the assignment; Twilio / Retell / Vapi diverge on cancel semantics, so the port leaves it out until a real adapter lands.
- **Global calls-per-second throttle.** Assignment specifies per-campaign concurrency only.
- **Inter-campaign weights.** Assignment specifies fairness within a campaign (retries first), not priority between campaigns.
- **Conversation engine** (TTS / STT / LLM). Lives behind a separate `ConversationEngine` port. This service's job ends at the telephony boundary.

---

## Run it

```bash
# Python 3.11+
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
make up                           # build + start Postgres + app
curl -s localhost:8001/health     # sanity check
open http://localhost:8001/ui     # operator UI
```

Host ports are `8001` (app) and `5442` (Postgres). Override with `HOST_APP_PORT` / `HOST_PG_PORT`. Useful `make` targets: `up`, `down`, `logs`, `reset-db`, `test`, `lint`, `format`.

---

## Try it

OpenAPI at `http://localhost:8001/docs`.

```bash
# Create a campaign.
curl -s -X POST http://localhost:8001/campaigns \
  -H 'content-type: application/json' \
  -d '{
    "name": "nyc-morning",
    "timezone": "America/New_York",
    "schedule": {"mon":[{"start":"09:00","end":"17:00"}]},
    "max_concurrent": 5,
    "retry_config": {"max_attempts": 2, "backoff_base_seconds": 30},
    "phones": ["+14155550001","+919876543210"]
  }'

# Per-campaign stats (the shape the assignment specifies).
curl -s http://localhost:8001/campaigns/<id>/stats
# -> {"total":2,"completed":1,"failed":0,"retries_attempted":1,"in_progress":1}

# Individual call status.
curl -s http://localhost:8001/calls/<id>

# Audit log.
curl -s 'http://localhost:8001/audit?event_type=DISPATCH&limit=50'
```

---

## Demo

Three `make` targets exercise the behaviors worth watching:

- **`make demo-reset`** — wipe campaigns / calls / audit for a clean chronology.
- **`make demo-fairness`** — seed two campaigns with different concurrency caps and a non-zero failure rate. Prints filtered `/ui` URLs showing dispatch counts tracking the concurrency ratio, retries beating new calls, and continuous channel reuse after completions.
- **`make demo-reclaim`** — seed one campaign, artificially age a `DIALING` row, and watch the reclaim sweep rescue it.

---

## Testing

```bash
make test   # unit + integration + e2e (testcontainers Postgres)
make lint   # ruff + mypy
```

The suite covers the scheduler invariants:

- Concurrency gate (no campaign exceeds `max_concurrent`).
- Retries beat new calls at the system level, with round-robin fairness across campaigns.
- Wake-driven reuse — a completed call is followed within milliseconds by the next dispatch.
- Business-hour close with in-flight calls draining naturally rather than being cancelled.
