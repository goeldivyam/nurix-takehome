---
name: Nurix.ai take-home project context
description: Principal Engineer take-home — outbound voice campaign microservice; interview defense ~30min with Mohit
type: project
---

Nurix.ai Principal Engineer take-home. Extends an existing outbound-voice-call microservice into a campaign system supporting: campaign lifecycle, business-hour scheduling with timezones, per-campaign concurrency, retry-before-new-call fairness, and status/stats tracking. Mock telephony integration is fine.

Interviewer: Mohit. Rubric axes observed so far: fairness, utilization, retry handling, starvation prevention, stuck-item recovery, concurrency, CPS limits, partial failures.

**Why:** This memory anchors what "good" looks like per the assignment PDF (/home/divyam/dev/nurix-takehome/context/assignment.pdf). Key spec-derived rules: (a) failed calls must be retried BEFORE new calls are initiated — this is stated at the system level, not per-campaign, which is an ambiguity to confirm with the interviewer; (b) concurrency limit has per-campaign default; (c) timezone-aware business hours required.

**How to apply:** When evaluating designs, always compare against the exact assignment language. "Retries before new calls" is the trap — designs that only do it per-campaign need a flag & a defended tradeoff.
