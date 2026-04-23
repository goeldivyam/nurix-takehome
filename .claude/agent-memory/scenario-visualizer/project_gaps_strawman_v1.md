---
name: Strawman v1 gaps surfaced
description: Gap status from first review pass on 2026-04-23; track which are fixed vs deferred vs accepted
type: project
---

First review pass on 2026-04-23. Verdict: **FIX-AND-ACCEPT**.

**Critical (must fix before implementation):**
- **G8 DB-connection exhaustion under retry storm** — single asyncpg pool shared by API+scheduler+webhook. Retry storm + webhook burst + API load = cascade failure. Status: OPEN.

**High (probable interview probes):**
- **G4 CPS back-pressure not specified** — "optional global CPS" is a bullet, not a mechanism. No 429 policy; risks burning retries_remaining on throttling. Status: OPEN.
- **G5 Worker crash + webhook race** — single process, and `UPDATE WHERE current_state=expected` silently no-ops when webhook arrives post-reclaim. Status: OPEN.
- **G6 Double-dial via stuck-reclaim** — reclaim issues new call_id while original call may still be live. Phone-number-level in-flight guard missing. 60s stuck threshold is too low for voice. Status: OPEN.

**Medium:**
- **G2 Cross-campaign retry priority** — DRR is symmetric across campaigns; assignment spec says "retries before new calls" at system level, not per-campaign. Need explicit decision. Status: OPEN.
- **G3 Business-hour boundary** — in-flight calls cross 21:00 boundary; no next-window wake-up specified. Status: OPEN.

**Low:**
- **G1 Whale-vs-minnows** — DRR handles it; minor CPS-vs-fairness interaction to document.
- **G7 Channel-reuse utilization** — correct by design; confirm webhook handler wakes scheduler synchronously.

**Why:** Tracking status so that review pass N+1 doesn't re-surface already-fixed gaps or generate scenarios already run. Also lets me generate FRESH adversarial scenarios each pass.

**How to apply:** On next review, first re-read this file, verify claimed fixes exist in code (grep/read), then hunt for gaps NOT on this list. Escalate to user if a gap marked OPEN still isn't addressed after 2 passes.

**Rehearse-first interviewer probe:** "Walk me through a 30-second provider blip causing 200 calls to flip to retry simultaneously. State at t=0, t=30s, t=60s." Covers G1, G4, G8.
