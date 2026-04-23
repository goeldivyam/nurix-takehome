---
name: Strawman v1 architecture decisions
description: Current committed design choices for the take-home — use as baseline for future review passes
type: project
---

Strawman v1 committed choices (as of 2026-04-23):

- **Stack:** Python 3.11 / FastAPI / asyncpg / Postgres in docker-compose. Single process, one event loop, API + scheduler + webhook handler co-located.
- **Scheduler:** Deficit Round Robin across campaigns, quantum=2. Within a campaign, retry queue has strict priority over new-call queue. Continuous channel reuse — every call state transition wakes the scheduler; periodic tick as safety net.
- **Claim mechanism:** `SELECT ... FOR UPDATE SKIP LOCKED`.
- **Limits:** Per-campaign concurrency + optional global CPS.
- **State machine:** Campaign PENDING→ACTIVE→(PAUSED↔ACTIVE)→COMPLETED|FAILED; Call QUEUED→DIALING→IN_PROGRESS→(COMPLETED|FAILED|NO_ANSWER); failed-with-retries→RETRY_PENDING→QUEUED. Atomic `UPDATE WHERE current_state = expected`.
- **Stuck recovery:** DIALING >60s gets reclaimed. (Flagged: threshold likely too low for voice calls.)
- **Provider:** Python Protocol `TelephonyProvider.place_call(call_id, phone)`; mock pushes events via `/webhooks/provider`. call_id = idempotency key.
- **Retries:** `retries_remaining + next_attempt_at + exp backoff + jitter`. Terminal errors skip retry.
- **Business hours:** per-campaign tz + hours; scheduler skips out-of-window at dispatch time.
- **Audit:** `scheduler_audit` table; events DISPATCH, SKIP_*, RETRY_DUE, WEBHOOK_RECEIVED, TRANSITION, RECOVERED_STUCK, CAMPAIGN_COMPLETED. `GET /audit` endpoint.

**Why:** User has committed to "simple and robust, not over-engineered" — explicitly rejecting Celery/Redis/RabbitMQ. Single-process is a deliberate simplicity choice for a take-home, defensible if framed as "horizontally scales by running N copies against same Postgres; SKIP LOCKED makes it safe."

**How to apply:** Before recommending any architecture change, check if it violates the simplicity principle. If user has said "simple and robust", do not propose Redis/Celery/Kafka unless a Critical gap forces it. Alternative fixes that stay within Postgres+FastAPI are preferred.
