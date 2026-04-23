---
name: Audit view must read as AI-native B2B, not terminal log
description: Theme direction for the /audit HTML surface — Linear/PostHog/Geist tier, not kubectl-style monospace terminal
type: feedback
---

The audit view theme must read as modern AI-native B2B (Linear, PostHog, Vercel Geist, Attio, Supabase Studio tier) — light-mode default, pure-neutral gray ramp, Inter + Geist Mono, 4-point grid, subtle 4–8px radii, two-level shadow system, 80–200ms motion.

**Why:** Divyam flagged on 2026-04-23 that an earlier draft of `/.claude/skills/audit-view-theme/SKILL.md` leaned too "raw terminal / kubectl-style" (dark-first, heavy mono, flat borders only). The take-home is evaluated by Mohit at Nurix.ai, who builds a modern voice-AI platform and will read the UI against contemporary AI-native B2B standards. Terminal aesthetics read as college-project / under-polished and undercut the Principal Engineer signal, even though the content (dense audit log) is correct.

**How to apply:**
- When reviewing or recommending UI for this project, the north star is Linear / PostHog / Attio polish, NOT Temporal event history raw text, NOT Datadog, NOT kubectl. Density stays high, but type, color, spacing, and motion are 2026-era.
- Light mode is default. Dark mode is optional and must not split design attention.
- Accents are single semantic steps (accent/success/warning/danger/info) — never multi-stop ramps, never decorative color.
- Motion is 80–200ms, one easing family, no bounces or pulses.
- Event tags: uppercase mono chips with ~10% alpha fill and solid semantic text color. Icons optional, Lucide-only, 12px, never decorative.
- The word "kubectl" is banned from this project's design vocabulary.
- Audit log remains the primary deliverable; a dashboard is progressive-disclosure layered on top of the same data, not a replacement.
