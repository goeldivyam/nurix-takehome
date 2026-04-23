---
name: Recurring weak spots to re-check every iteration
description: Gaps flagged across design reviews that tend to re-emerge; check these on every new submission
type: project
---

Weak spots that recurred or remain open as of 2026-04-23 V2 freeze:

1. **TTS/STT/LLM integration boundary** — weakest gap for a voice-AI role. V2 Provider Protocol stops at telephony; conversation engine placement not specified.
2. **Provider-dispatch crash window** — the claim-then-place_call gap is the honest at-least-once boundary; candidate must concede it cleanly, not paper over.
3. **Cost and 100x scale economics** — not pre-baked in V2; interviewer-friendly question that candidate hasn't rehearsed a crisp answer to.
4. **WRR tie-breaker rule** when weights collide — unspecified; easy nit for a hostile interviewer.
5. **Audit-write atomicity** with state transitions — not explicitly same-transaction in all paths.
6. **Dead-letter / poison-event handling** on webhook_inbox — absent.
7. **Mid-flight campaign config edits** (weight change, CPS change, pause-during-dispatch race) — not addressed.

**Why:** These are the first things a Nurix principal interviewer will probe. Some are acceptable concessions at take-home scope; candidate should have a one-line answer for each ready.

**How to apply:** On any future review of this project, explicitly check whether these have been addressed, conceded, or left silent. Silent is the worst option.
