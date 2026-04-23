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
| `app/scheduler` | WRR rotation + retry priority + concurrency / schedule gates |
| `app/provider` | `TelephonyProvider` Protocol + mock implementation |
| `app/state` | Campaign + call state machines; sole mutator |
| `app/audit` | Event writer + reader — observability surface |

## Scheduler policy (locked)

Each tick is a pipeline of filters:

1. **Eligibility**: campaign is ACTIVE, in business hours right now, has work to do.
2. **Retry sweep**: any campaign has a `RETRY_PENDING` call whose `next_attempt_at` has passed → dispatch that first.
3. **Concurrency gate**: skip any eligible campaign already at its `max_concurrent`.
4. **Round-robin pick**: among survivors, pick in stable order (by `campaign_id`, cycling). No weights — the assignment does not specify priority between campaigns.
5. **Dispatch**: write audit row with full reasoning (why this call, why now).

**Business hours** = per-campaign timezone + weekly calendar. Each day has zero or more `[start, end]` windows (e.g. Mon–Fri 14:00–16:00 + 20:00–22:00; Sat 11:00–13:00; Sun 18:00–20:00). Each day independent. Single predicate: "is current local time inside any of today's windows?"

Weights are intentionally omitted — the assignment specifies per-campaign `max_concurrent`, per-campaign retry config, and per-campaign business hours, but says nothing about one campaign having priority over another. "Fairness" in the spec means retries-before-new *inside* the queue, not weighted share between campaigns. Adding a weight field later is a one-line extension; document as future work in README.

## Provider abstraction

The assignment's existing service exposes two APIs: trigger a call, check call status. Our `app/provider/` layer wraps both:

- `TelephonyProvider.place_call(idempotency_key, phone) → CallHandle` — thin wrap over "trigger" API.
- `TelephonyProvider.get_status(call_id) → CallStatus` — thin wrap over "check status" API (also the confirm path for stuck-reclaim).
- Mock also **pushes** status transitions as webhook events to `/webhooks/provider` for event-driven updates. Polling remains available as a fallback.

The rest of the system talks only to the port — never to the mock directly.

## State model

- **Campaign**: `PENDING → ACTIVE → COMPLETED | FAILED`
- **Call**: `QUEUED → DIALING → IN_PROGRESS → COMPLETED | FAILED`; failed-with-retries-left → `RETRY_PENDING → QUEUED` (requeued when `next_attempt_at` reached).
- Every transition: atomic `UPDATE WHERE id=$id AND status=$expected AND attempt_epoch=$expected`.

## Crash safety

- `call.attempt_epoch` (int) increments on every retry and every reclaim.
- **Stuck reclaim**: `DIALING` > `max_call_duration + 30s` → reset to `QUEUED` with bumped epoch.
- **Idempotency key** at provider port = `f"{call_id}:{attempt_epoch}"`.
- **Phone-level in-flight guard**: unique partial index on `(phone) WHERE status IN ('QUEUED','DIALING','IN_PROGRESS')`.
- **Webhook**: ack-then-process via `webhook_inbox` with `provider_event_id UNIQUE`.

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
