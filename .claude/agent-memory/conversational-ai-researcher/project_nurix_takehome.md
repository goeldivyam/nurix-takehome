---
name: Nurix take-home scope and rubric
description: Nurix.ai Principal Engineer take-home — outbound voice campaign microservice, local-only, Python/FastAPI/asyncpg/Postgres. Captures evaluator rubric and strawman decisions.
type: project
---

Nurix.ai Principal Engineer take-home: outbound voice campaign microservice. Local-only, single docker-compose, Python 3.11 / FastAPI / asyncpg / Postgres. Mock telephony provider only.

**Why:** Evaluator is Mohit; 8-point rubric — correctness, state-model clarity, scheduler quality, fairness+utilization (continuous channel reuse, NOT batch-synchronous), retry handling (retry-before-new), abstraction quality, observability (audit log is sufficient), practical systems judgment. Principle: simple and robust, NOT over-engineered.

**How to apply:**
- Do not push toward Temporal/Inngest/Celery/Redis — Postgres + asyncio only.
- Continuous event-driven dialing is required; flag any batch-and-wait pattern as an automatic rubric fail.
- Retry queue must have strict priority over new-call queue.
- Audit log table is the visualization — no dashboards needed.
- For fairness: weighted RR is usually the right answer at take-home scale; DRR only if the candidate defends it in the README.
- Webhook handling needs ack-then-process with an inbox table; attempt_epoch on call row to drop stale late events.
- Stats aggregation endpoint is required (rubric item 5), not optional.
