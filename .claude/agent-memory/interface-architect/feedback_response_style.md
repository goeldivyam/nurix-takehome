---
name: Response style — terse, bounded, no implementation code
description: User expects tight, word-budgeted architectural reviews with concrete signatures in pseudocode only
type: feedback
---

Keep architectural reviews tight and bounded — user specifies word limits (e.g. "under 500 words") and expects them honored.

**Why:** User is senior, drafting take-home architecture docs under time pressure. They want the specific leak/missing-port/verdict, not a tutorial.

**How to apply:**
- Honor explicit word budgets.
- Follow the exact output format the user requests (summary paragraph, numbered answers, verdict) rather than the default 5-section template when they override it.
- Pseudocode signatures only — never produce implementation code.
- Name specific methods/types/fields — vague advice like "improve abstraction" is useless.
- Give a clear verdict (READY / NEEDS-TIGHTER-CONTRACTS / RESHUFFLE) with named blockers when asked.
