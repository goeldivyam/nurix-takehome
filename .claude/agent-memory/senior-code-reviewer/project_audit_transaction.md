---
name: Audit transaction invariant
description: State transition + audit row must share one transaction on the caller's connection; audit_pool is reader-only
type: project
---

Audit rows must be written on the SAME connection and inside the SAME transaction as the state transition they describe. `audit_pool` exists only for readers (observability UI). There is no "emit audit asynchronously" or "audit_pool.execute after transition commit" path.

**Why:** Two rules in CLAUDE.md/backend-conventions are in tension: "audit log reads only, never on the scheduling critical path" (CLAUDE.md:30) vs "state transition + audit row must be in the same transaction. If audit fails, transition rolls back" (backend-conventions SKILL:109). The trap: a developer resolves the tension by decoupling the audit write, which either loses audit events on crash (breaks rubric #7 — "audit log IS the visualization") or double-acquires the pool (deadlock under load).

**How to apply:** During review, reject any `audit_pool.execute(...)` / `audit_pool.fetchval(...)` inside a write path. Reject any `create_task(emit_audit(...))` after a transition. The pattern to accept: repository function takes `conn` and issues both the CAS UPDATE and the audit INSERT inside `async with conn.transaction():`. Flag this as Critical when it regresses — it is the single most load-bearing invariant in this codebase.
