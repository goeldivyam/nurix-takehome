---
name: Top-3 PR rejection patterns
description: The three patterns that auto-reject a PR in this codebase — state mutation outside state/, provider boundary leaks, double-acquire
type: feedback
---

Three patterns auto-reject any PR in this codebase, regardless of how much else is right:

1. **Status mutation outside `app/state`**. Any `UPDATE calls SET status=...` or `UPDATE campaigns SET status=...` in api/scheduler/provider/audit is a reject. `state.transition(...)` is the sole mutator.
2. **Provider boundary leaks**. Scheduler importing provider types, or provider code referencing `campaign_id` / retry policy / fairness / business hours, is a reject. The `TelephonyProvider` Protocol is the entire contract.
3. **Double-acquire inside `async with pool.acquire()`**. Any helper that does `pool.fetchval(...)` / `pool.execute(...)` while a connection from the same pool is already held is a reject — deadlock risk at pool size.

**Why:** These three map directly to the rubric: #2 state-model clarity (rejection 1), #6 abstraction quality (rejection 2), #1 correctness (rejection 3). They are also the three traps most likely to appear in a take-home under time pressure.

**How to apply:** On any scheduler/state/api/provider diff, grep for these three shapes first, before any other review pass. If any hit, report as Critical and stop — no need to list other findings until these are resolved.
