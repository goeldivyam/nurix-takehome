# CLAUDE.md

## Project

Nurix.ai Principal Engineer take-home: **outbound voice campaign microservice**. Local-only, docker-compose. Python 3.11 / FastAPI / asyncpg / Postgres. Mock telephony.

The assignment (`context/assignment.pdf`) asks us to extend an existing service that can place one call and check its status. We add **campaigns** — groups of phone numbers dialed together with business-hour scheduling, per-campaign concurrency, retries-before-new, and status / stats tracking.

## Evaluation rubric (ground truth)

Judged against Mohit's 8 points:

1. Correctness
2. State-model clarity
3. Scheduler quality
4. Fairness + utilization (continuous channel reuse — NOT batch-synchronous)
5. Retry handling (failed retry before new calls at the SYSTEM level)
6. Abstraction quality
7. Observability — **the audit log IS the visualization**; dashboard is optional polish
8. Practical systems judgment

## Non-negotiables

- **Simple and robust; not over-engineered.** Every extra primitive must map to a rubric point or an explicit assignment line.
- **Scheduler is a first-class module.** Not a Celery config. Named policy, testable, auditable.
- **Retries beat new calls at the SYSTEM level**, not per-campaign (assignment's literal words).
- **Continuous channel reuse.** Every call completion wakes the scheduler; no batch-wait.
- **Provider is black-boxed.** Scheduler never imports provider code; provider never knows about campaigns, fairness, or retries.
- **State machine is the sole mutator** of campaign + call rows. API and scheduler call `state.transition(...)`, never `UPDATE` directly.
- **Every provider side-effect carries an idempotency key** = `f"{call_id}:{attempt_epoch}"`.
- **Audit log reads only.** Never on the scheduling critical path.
- **Always use `.venv/` locally.** Never run Python against system Python. Activate `source .venv/bin/activate` before any local `python`, `pytest`, `pip`, `ruff`, `mypy`. Docker handles its own env inside the container; this rule is about local dev.

## The 6 subsystem layers

| Layer | Responsibility |
|---|---|
| `app/api` | FastAPI routes — campaign CRUD, status, stats, webhook |
| `app/persistence` | asyncpg pools + repositories — data access |
| `app/scheduler` | Round-robin rotation + retry priority + concurrency / schedule gates |
| `app/provider` | `TelephonyProvider` Protocol + mock implementation |
| `app/state` | Campaign + call state machines; sole mutator |
| `app/audit` | Event writer + reader — observability surface |

**Shared types — owned by a specific layer** to prevent duplication across modules:

- `CallStatus` enum — owned by `app/state/`, imported by provider / audit / api. Closed set: `{DIALING, IN_PROGRESS, COMPLETED, FAILED, NO_ANSWER, BUSY}`. Adapter-side translation (e.g. Twilio's `canceled` → `FAILED`) is the provider port's contract, not the core's.
- `AuditEvent` dataclass — owned by `app/audit/`, imported by state / scheduler.

## Scheduler policy (locked)

Each tick is a pipeline of filters:

1. **Eligibility**: campaign is ACTIVE, in business hours right now, has work to do.
2. **Retry sweep**: any campaign has a `RETRY_PENDING` call whose `next_attempt_at` has passed → dispatch that first.
3. **Concurrency gate**: skip any eligible campaign already at its `max_concurrent`.
4. **Round-robin pick**: among survivors, pick in stable order (by `campaign_id`, cycling). No weights — the assignment does not specify priority between campaigns.
5. **Dispatch**: write audit row with full reasoning (why this call, why now).

**Business hours** = per-campaign timezone + weekly calendar. Each day has zero or more `[start, end]` windows (e.g. Mon–Fri 14:00–16:00 + 20:00–22:00; Sat 11:00–13:00; Sun 18:00–20:00). Each day independent. Single predicate: "is current local time inside any of today's windows?"

Weights are intentionally omitted — the assignment specifies per-campaign `max_concurrent`, per-campaign retry config, and per-campaign business hours, but says nothing about one campaign having priority over another. "Fairness" in the spec means retries-before-new *inside* the queue, not weighted share between campaigns. Adding a weight field later is a one-line extension; document as future work in README.

**Scheduler wake signal** — named port `SchedulerWake` owned by `app/scheduler/`, exposing `notify()` and `async wait(timeout)`. State machine calls `notify()` after every terminal transition; webhook processor calls `notify()` after each inbox dequeue + transition. Implementation = a module-level `asyncio.Event`. Dependency-injected into state + webhook code; in-memory fake for tests.

**Loop shape (non-negotiable, avoids lost-wakeup)**:
```
await wake.wait(timeout=safety_net_seconds)
wake.clear()          # clear BEFORE tick so any notify during tick is captured for the next iteration
await tick()
```
Never `clear()` after `tick()`, never `clear()` before `wait()`. A `notify()` arriving during `tick()` sets the flag; the next `wait()` returns immediately. The safety-net timeout handles the rare case where no notify arrives (all campaigns quiet).

## Provider abstraction

The assignment's existing service exposes two APIs: trigger a call, check call status. Our `app/provider/` layer wraps both:

- `TelephonyProvider.place_call(idempotency_key, phone) → CallHandle` — thin wrap over "trigger" API.
- `TelephonyProvider.get_status(call_id) → CallStatus` — thin wrap over "check status" API (also the confirm path for stuck-reclaim).
- Mock also **pushes** status transitions as webhook events to `/webhooks/provider` for event-driven updates. Polling remains available as a fallback.

The rest of the system talks only to the port — never to the mock directly.

**`CallHandle` shape** (returned by `place_call`): `{ provider_call_id: str, accepted_at: datetime }`. **Provider errors** are typed exceptions owned by `app/provider/`: `ProviderRejected(reason_code)` for expected rejections (invalid number, blocked), `ProviderUnavailable` for infrastructure failures. Scheduler and state catch these exception types — never provider-specific errors.

**Webhook payload → `ProviderEvent` translation** is the adapter's responsibility (mock: a module-level `parse_event(payload) → ProviderEvent` in `app/provider/mock.py`). The webhook processor dequeues from `webhook_inbox`, calls the adapter's parse helper, then `state.transition(...)`. Translation stays in the adapter, never in the API layer. Moving `parse_event` onto the `TelephonyProvider` Protocol becomes useful only when a second adapter lands.

**Webhook signature verification** is also the adapter's responsibility — `verify_signature(headers, raw_body) → bool`. Production providers (Twilio, Retell, Vapi) sign their webhooks; the API webhook endpoint MUST call this before enqueuing to `webhook_inbox`. Mock returns `True` unconditionally (local-only, no secret). Production adapters validate HMAC / signing key per provider docs.

Future extension points (README future-work, not built): `cancel(call_id)` for campaign abort; `parse_event(payload) → ProviderEvent` on the Protocol when a second adapter (Twilio / Vapi) lands. Adding these when they're needed beats guessing their shape today — Twilio / Vapi / Retell all diverge on cancel semantics.

**Conversation engine (TTS / STT / LLM) is out of scope for this service.** It sits behind a separate port (`ConversationEngine`) invoked by the telephony provider on media events, not by our scheduler. The campaign layer passes `script_ref` down through `place_call`; the engine resolves the audio loop independently. This service's job ends at the telephony boundary.

## State model

- **Campaign**: `PENDING → ACTIVE → COMPLETED | FAILED`
- **Call**: `QUEUED → DIALING → IN_PROGRESS → COMPLETED | FAILED`; failed-with-retries-left → `RETRY_PENDING → QUEUED` (requeued when `next_attempt_at` reached).
- Every transition: atomic `UPDATE WHERE id=$id AND status=$expected AND attempt_epoch=$expected`.

**Retry classification** — which failures are retryable:

- **Terminal provider errors** (invalid number, blocked, rejected) → `FAILED` directly. No retry.
- **Transient provider errors**, **`NO_ANSWER`**, **`BUSY`** → `RETRY_PENDING` with `next_attempt_at = NOW() + backoff`.
- **Backoff** = `base * 2^attempt` seconds with ±20% jitter; `base` and `max_attempts` from campaign `retry_config`.
- When `retries_remaining` hits 0 → `FAILED`.

## Crash safety

- `call.attempt_epoch` (int) increments on every retry and every reclaim.
- **Stuck reclaim** runs on a background sweep, NEVER on the dispatch critical path. When `DIALING` exceeds `max_call_duration + 30s`, FIRST call `provider.get_status(call_id)` as a **best-effort confirm** (configurable timeout `STUCK_RECLAIM_GET_STATUS_TIMEOUT_SECONDS`). Correctness does NOT depend on bypassing provider-side caches — it depends on the grace window being wider than any plausible provider cache TTL plus the epoch CAS at apply time. On timeout or error, treat the result as unknown and proceed to reclaim. If the provider reports a terminal state (COMPLETED / FAILED / NO_ANSWER / BUSY), apply that outcome **on the SAME `attempt_epoch`** (no bump) — do NOT reclaim. Only reclaim (reset to `QUEUED`, bump `attempt_epoch`) when the provider returns unknown / still-dialing. The sweep fans out across stuck rows via `asyncio.TaskGroup`, but **each per-row task catches its own exceptions and returns a typed `ReclaimOutcome` Result** — exceptions never escape into TaskGroup (which would cancel siblings and lose their in-flight results). A single slow or crashing `get_status` never head-of-line-blocks the sweep. **UPDATE the same row in place** — never INSERT + DELETE; the phone-level partial unique index would collide on INSERT.
- **Idempotency key** at provider port = `f"{call_id}:{attempt_epoch}"`.
- **Phone-level in-flight guard**: unique partial index on `(phone) WHERE status IN ('QUEUED','DIALING','IN_PROGRESS')`.
- **Webhook**: ack-then-process via `webhook_inbox` with `provider_event_id UNIQUE`.
- **Webhook ordering is NOT guaranteed** by providers (Twilio's `statusCallback` explicitly doesn't). The state machine's CAS on `(status, attempt_epoch)` silently no-ops for stale or out-of-order events — but `webhook_inbox` PERSISTS every accepted event. Stale events remain queryable for forensics (the classic "lost webhook" debugging story). **Accepted consequence**: if a terminal event arrives before an intermediate one within the same epoch, the intermediate audit row is dropped (the state already moved past it). Terminal state always wins; intermediate audit coverage is best-effort — fine for observability, not load-bearing for correctness.
- **Audit atomicity**: every state transition and its audit row are written on the same connection inside the same transaction. Readers never observe a transition without its reason. `audit_pool` exists strictly for observability reads — never for writes on the critical path.

## Development workflow

1. Read relevant files, check rubric alignment, write a short plan to `tasks/todo.md`.
2. Check in with Divyam — confirm before implementation.
3. Build one module at a time, marking done as you go.
4. After each module: invoke `senior-code-reviewer` before commit.
5. Before submission: invoke `interviewer-lens-reviewer` as the final gate.

## Pre-commit checks

Configured in `.pre-commit-config.yaml` + `pyproject.toml`:

- **Hygiene**: trailing whitespace, end-of-file, YAML/TOML syntax, merge-conflict markers, oversized files.
- **Python**: `ruff` (lint + auto-fix) + `ruff-format` (replaces black / flake8 / isort / pyupgrade in one tool). Config in `pyproject.toml` — includes the `ASYNC` ruleset, which catches the sync-in-async anti-patterns called out in `code-quality` and `backend-conventions` skills.
- **Frontend ESLint**: stub is commented out in `.pre-commit-config.yaml`; uncomment when `frontend/` is added.

**One-time setup** (inside `.venv`):
```bash
pip install pre-commit
pre-commit install
```

**Rule**: NEVER pass `--no-verify` to `git commit`. If a hook fails, fix the underlying issue.

## Git commit discipline

**Commits are written for the evaluator, not for our internal process.** The commit history is a design-decision log a reviewer can walk through to see how the system evolved. Never reference internal workflow artifacts (agent names, iteration numbers, review-process shorthand). Describe the DESIGN CHANGE and the reason, in design terms.

- **One logical change per commit.** Unrelated edits go in separate commits.
- **Subject ≤ 50 chars, imperative mood** (`Harden reclaim against stale cache`, not `Fixed the reclaim stuff`). Blank line, then a body.
- **The body explains the WHY** — a future reader shouldn't need conversation history. Cite the design reason: a concrete failure scenario, a rubric dimension, an assignment requirement, a production pattern (Twilio / Retell / Vapi style) — never who surfaced it internally.
- **Never bypass pre-commit** (`--no-verify`). Fix the hook failure.
- **Never amend already-pushed commits.** Create a new commit instead.

## Parallel work — Git worktrees

When working on multiple independent changes at the same time (different modules, different experiments, different agent-driven explorations), use `git worktree` — not multiple clones, not stash-and-switch.

**Create a new worktree:**
```bash
git worktree add ../nurix-takehome-<feature-name> -b <feature-name>
cd ../nurix-takehome-<feature-name>
```

**Isolation checklist for every new worktree:**
1. **Fresh venv per worktree** — `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`. Never symlink `.venv`.
2. **Copy `.env`** — gitignored, doesn't follow the worktree: `cp ../nurix-takehome/.env .env`.
3. **Bump ports** — each worktree's docker-compose must not collide with another's. Offset per tree: tree 1 = 8000/5432, tree 2 = 8010/5442, tree 3 = 8020/5452.
4. **Separate docker-compose project name** — each worktree directory is its own compose project by default, so this works out of the box if you don't hardcode `--project-name`.
5. **Separate IDE / terminal per worktree** — launch Claude Code from inside the worktree directory so file tools stay scoped.

**Cleanup when done:**
```bash
git worktree remove ../nurix-takehome-<feature-name>
```
(Merged branches get deleted separately via the normal PR flow.)

**Shared state to watch for:** `.git/hooks` is shared across worktrees; any hook change affects all of them.

**When not to use a worktree:** truly quick fixes (seconds-minutes scale). For anything bigger, use a worktree.

## Tech choice defense (README must articulate these)

- **Postgres only** (no RabbitMQ, no Celery, no Redis): `SELECT ... FOR UPDATE SKIP LOCKED` gives reliable queue semantics; one fewer moving part at take-home scope.
- **Single process** (API + scheduler + webhook in one event loop): avoids cross-process coordination; scales horizontally by running N replicas against the same Postgres.
- **asyncpg** (not psycopg2): required for async non-blocking I/O.

## Agent dispatch table

| Situation | Agent |
|---|---|
| Need industry / voice-AI patterns | `conversational-ai-researcher` |
| Stress-test a design scenario | `scenario-visualizer` |
| Review module / interface boundaries | `interface-architect` |
| Design / review operator-facing surface | `b2b-ui-ux-advisor` |
| Review written code | `senior-code-reviewer` |
| Final rubric score + interview defense | `interviewer-lens-reviewer` |

Invoke agents in parallel when asks are independent. After any meaningful code chunk, run `senior-code-reviewer` before commit.

**Every agent brief MUST begin with**: *"Before responding, re-read `context/assignment.pdf` and `CLAUDE.md`."* No exceptions — agents drift as the design evolves; re-reading keeps their feedback grounded in the current locked state, not their memory of an older version. Remind them to paste findings INLINE (not memory-only), and to severity-tag every finding (Critical / Important / Polish).

## Skills

- `update-docs` — run after any meaningful change to keep docs in sync (invoke via `/update-docs`).
- `architecture-overview` — current file-level map; update as modules land.
- `code-quality` — naming, dead code, async discipline.
- `backend-conventions` — asyncpg, SKIP LOCKED, transactions, timezone rules.

## Key reminders

- Do what's asked — nothing more, nothing less.
- Production-quality code; no `TODO` comments in submitted code.
- Never create files unless necessary; prefer editing existing.
- No generated docs unless explicitly asked.
- Never introduce OWASP-top-10 vulnerabilities.
