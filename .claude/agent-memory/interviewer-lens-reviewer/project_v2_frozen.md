---
name: V2 architecture — frozen after round 1
description: Key design decisions in the V2 architecture that was cross-reviewed and pre-freeze-gated on 2026-04-23
type: project
---

V2 architecture for the Nurix take-home was scored defense-ready on 2026-04-23 after cross-review by conversational-ai-researcher, scenario-visualizer, interface-architect, b2b-ui-ux-advisor, and this reviewer.

**Why:** Design freeze gate before implementation begins. All 8 rubric points at Strong except Abstraction (Adequate — TTS/STT boundary gap).

**How to apply:** Treat these as the candidate's committed positions. If future iterations contradict them, flag the inconsistency. Key committed decisions:
- Single-process FastAPI + asyncpg + Postgres; no Celery/Redis
- WRR scheduler with DRR explicitly rejected as over-engineered at <20 campaign scale
- System-level retry priority (retries beat new calls across campaigns, still weighted)
- attempt_epoch + CAS updates + phone partial unique index for idempotency
- Webhook ack-then-process via inbox table with provider_event_id UNIQUE
- Stuck reclaim at max_call_duration + 60s (default 10 min — NOT 60s)
- 3 asyncpg pools (API read / scheduler write / webhook ack)
- Audit log is the primary observability surface; dashboard is optional polish
- In-flight calls at business-hours-close continue to natural end (documented policy)

**Five rehearsed defense questions:** crash window between claim and place_call; retry-priority fairness tradeoff; 3am debug via audit log; TTS/STT/LLM boundary (weakest); 100x scale breakpoints.
