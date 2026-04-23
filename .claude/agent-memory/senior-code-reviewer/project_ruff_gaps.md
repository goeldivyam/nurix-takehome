---
name: Ruff config gaps
description: DTZ and SIM are the high-value ruff rule families missing from pyproject.toml
type: project
---

The current ruff select is `E, W, F, I, N, UP, B, ASYNC, RUF`. Two high-value additions were flagged during the pre-implementation standards audit:

- **`DTZ` (flake8-datetimez)** — bans naive `datetime.now()` / `datetime.utcnow()`. Directly enforces the timezone discipline codified in code-quality SKILL:91 and backend-conventions SKILL:125. Timezone correctness is a rubric #1 surface because business-hour checks are per-campaign-timezone.
- **`SIM` (flake8-simplify)** — low-noise readability cleanups (truthy-return collapses, nested-with collapses). Backs the "readability" signal in the rubric.

Not added: `TRY` (tryceratops) was considered but rejected as noisy for take-home scope; `B` + code review catch the broad-except cases that matter.

**Why:** Pre-implementation audit on 2026-04-23 identified these as the gaps most likely to bite during build. DTZ in particular would catch a whole class of business-hour bugs at lint time instead of in a reviewer's head.

**How to apply:** If reviewing a diff and a naive `datetime.now()` slips in, flag it Critical and reference that `DTZ` would have caught it automatically — this is a cue to Divyam to add the ruleset. Do not re-flag if he has already declined to add DTZ.
