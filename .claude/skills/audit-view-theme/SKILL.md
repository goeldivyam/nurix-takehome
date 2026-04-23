---
name: audit-view-theme
description: Design theme for the optional operator-grade HTML view over GET /audit. Palette, typography, layout primitives, event-type categorization, forbidden anti-patterns. Load when building or reviewing the audit UI.
---

# Audit view — design theme

The audit log is the visualization deliverable; a dashboard is optional polish. If the HTML view is built, it must read as an operator tool — dense, factual, scannable. Reference line: `kubectl get events`, Temporal event history, PostHog activity log. **Not** Datadog, Mixpanel, or Vercel.

## Palette (muted operator, dark-first)

No gradients anywhere. No glows. No soft shadows beyond a 1px border.

| Token | Hex | Role |
|---|---|---|
| `--bg` | `#0B0D10` | Page background |
| `--bg-elevated` | `#111418` | Table zebra / sticky bar |
| `--border` | `#1E2329` | Default border |
| `--border-strong` | `#2A3139` | Hover / emphasis border |
| `--fg` | `#E6E8EB` | Primary text |
| `--fg-muted` | `#9AA3AD` | Metadata |
| `--fg-dim` | `#5C6672` | Tertiary hints (italics only) |
| `--accent-blue` | `#4C8BF5` | Dispatch / informational |
| `--accent-amber` | `#C9953D` | Skip / gated (normal, not alarming) |
| `--accent-red` | `#C85450` | Terminal failure / reclaim bump |
| `--accent-green` | `#6FAE6A` | Terminal success / campaign complete |
| `--accent-violet` | `#8A7BD1` | Retry-due |

Accents are a single desaturated step — never multi-stop ramps.

## Typography

- **Log rows, timestamps, IDs, reason payload**: `ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace`, 12–13px, line-height 1.45.
- **Filter bar, headers, empty/error copy**: `-apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", system-ui, sans-serif`, 13–14px.
- Weights: 400 / 500 / 600 only.
- No italics except on `--fg-dim` metadata hints.

## Layout primitives

- **Sticky top filter bar** — campaign multi-select, event-type checkboxes, time range, free-text filter over `reason`. 1px `--border` bottom.
- **Dense table** directly below the filter bar. Columns: `ts | campaign | call_id | event | reason`. Fixed-width mono columns for `ts` and `call_id`; flex for `reason`. Row height ~28px. Zebra via `--bg-elevated` on odd rows.
- **Hover** = full-row background shift to `--border`.
- **Click-to-pin** — clicking a row drops it into a persistent top strip (max 5 pinned, each dismissable). Lets operators hold context while scrolling.
- **Empty state** — centered mono line "no events match current filters" + a "clear filters" text link. No illustrations.
- **Loading state** — single 1px indeterminate bar under the filter bar. No spinners.
- **Error state** — inline red-bordered strip at top with the failing endpoint and a retry link. No toast.

## Event-type categorization

Each row gets a 2px left-edge color bar + a 10-char uppercase mono tag in a fixed column. **Never icons.** Color is secondary; the tag is authoritative.

| Event type | Color | Notes |
|---|---|---|
| `DISPATCH` | blue | informational |
| `SKIP_CONCURRENCY` / `SKIP_BUSINESS_HOUR` / `SKIP_RETRY_BACKOFF` | amber | skips are normal, not alarming |
| `RETRY_DUE` | violet | |
| `WEBHOOK_RECEIVED` | `--fg-muted` | |
| `WEBHOOK_IGNORED_STALE` | amber | dropped-by-CAS forensic marker |
| `TRANSITION` | `--fg` | neutral; the `reason` payload carries the signal |
| `RECLAIM_SKIPPED_TERMINAL` | green | |
| `RECLAIM_EXECUTED` | red | bumped epoch, will redial |
| `CAMPAIGN_COMPLETED` | green | |

## Forbidden

- No animations beyond a 150ms indeterminate loading bar.
- No donut / pie / radial charts anywhere.
- No live-tail auto-scroll pulse, no "new event" flash, no confetti.
- No AI / sparkle / brain icons. No emoji in event tags.
- No toast notifications for normal events.
- No rounded-xl cards, no pastel badges, no soft shadows.
- No playful failure copy ("Oops!", "You're all caught up!", "Nothing to see here"). Errors read factually: `request failed: 503 from /audit`.
