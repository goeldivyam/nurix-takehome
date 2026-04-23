---
name: Nurix.ai outbound voice campaign take-home
description: Principal Engineer take-home — local-only outbound voice campaign microservice in Python/FastAPI/asyncpg/Postgres
type: project
---

Nurix.ai Principal Engineer take-home: local-only outbound voice campaign microservice.

**Why:** User is working through a take-home where evaluators weigh abstraction quality heavily. Subsystems: campaign mgmt, scheduler (DRR + SKIP LOCKED claim), state machine (campaign + call), telephony provider (black-box behind port), persistence (asyncpg), API (FastAPI), audit/observability.

**How to apply:** Prioritize clean seams over cleverness — "simple and robust, not over-engineered" is the stated principle. The strawman stack is Python 3.11 / FastAPI / asyncpg / Postgres / docker-compose, single process, API+scheduler in one event loop. Assignment PDF at `context/assignment.pdf`. Team size assumed at 3–4 parallel contributors.

Non-negotiable invariants the user has already committed to:
- Scheduler must not import provider code.
- Provider adapter knows nothing about campaigns/fairness/retries.
- State machine is sole mutator; API and scheduler go through `state.transition(call_id, expected, new)`.
- Every provider side effect carries an idempotency key (call_id is the default choice).
- Audit reads from audit tables only; never on scheduling critical path.
