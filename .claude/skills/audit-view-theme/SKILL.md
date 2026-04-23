---
name: audit-view-theme
description: Design theme for the operator-grade HTML view over GET /audit. Enterprise AI-native B2B aesthetic — light-mode default, Linear / PostHog / Geist tier. Load when building or reviewing the audit UI.
---

# Audit view — design theme

The audit log is the visualization deliverable; a dashboard is optional polish. If a UI is built, it must read as a modern AI-native B2B operator console — dense information, restrained palette, deliberate typography. Reference tier: [Linear](https://linear.app), [PostHog Activity](https://posthog.com/docs/activity), [Vercel Geist](https://vercel.com/geist/introduction), [Attio](https://attio.com), [Supabase Studio](https://supabase-design-system.vercel.app). The surface is an operator console, not a marketing page — but it is 2026-era polished, not raw terminal.

## Foundations

- **Base unit**: 4-point grid. Spacing scale `4, 8, 12, 16, 20, 24, 32, 40, 48`. Nothing off-grid.
- **Radius scale**: `4px` (inputs, tags, inline chips), `6px` (buttons, table-row hover highlight), `8px` (cards, modals). No pill shapes outside status tags.
- **Border width**: `1px` default. No double borders, no inset shadows for separation.
- **Elevation**: exactly two shadow tokens. `shadow-sm` for sticky filter bar (`0 1px 2px rgba(17, 24, 28, 0.04)`), `shadow-md` for pinned-row strip and popovers (`0 4px 12px rgba(17, 24, 28, 0.06), 0 1px 2px rgba(17, 24, 28, 0.04)`). Nothing heavier.
- **Focus ring**: 2px `--accent` with 2px offset. Visible, not decorative.

## Palette — light mode default

Pure-neutral gray ramp (no warm or cool bias, Geist-style). Accents are single semantic steps, not multi-stop ramps. Dark mode is an optional mirror — do not ship it if it splits attention.

| Token | Light | Dark (optional) | Role |
|---|---|---|---|
| `--bg` | `#FFFFFF` | `#0A0A0A` | Page background |
| `--bg-subtle` | `#FAFAFA` | `#111111` | Filter bar, sticky header |
| `--bg-hover` | `#F4F4F5` | `#171717` | Row hover, button hover |
| `--bg-active` | `#EDEDEE` | `#1F1F1F` | Pressed / selected row |
| `--border` | `#E4E4E7` | `#232323` | Default hairline |
| `--border-strong` | `#D4D4D8` | `#2E2E2E` | Input border, focus within |
| `--fg` | `#0A0A0A` | `#EDEDED` | Primary text |
| `--fg-muted` | `#52525B` | `#A1A1AA` | Secondary metadata |
| `--fg-subtle` | `#8A8A93` | `#71717A` | Tertiary hints, placeholders |
| `--accent` | `#2563EB` | `#3B82F6` | Links, primary action, focus |
| `--success` | `#16A34A` | `#22C55E` | Terminal success, reclaim-skipped |
| `--warning` | `#D97706` | `#F59E0B` | Skips, stale webhook, backoff |
| `--danger` | `#DC2626` | `#EF4444` | Reclaim executed, failure |
| `--info` | `#7C3AED` | `#8B5CF6` | Retry-due (violet, distinct from accent) |

Contrast: every pair above clears WCAG AA at 14px. Never use accent colors for decoration — only to carry meaning.

## Typography

- **UI / labels / headers**: `Inter`, fallback `-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif`. Feature settings: `"cv11", "ss01", "tnum"` (tabular numerals, stylistic alt 1, single-story g). [Geist Sans](https://vercel.com/font) is an acceptable substitute.
- **Data / timestamps / IDs / reason payload**: `"Geist Mono"`, fallback `"JetBrains Mono", "SF Mono", ui-monospace, monospace`. Tabular.
- **Scale**: `11px` (micro caps), `12px` (dense table), `13px` (body, filter controls), `14px` (primary labels), `16px` (page title), `20px` (section heading, rare). Line-heights: `1.4` for dense rows, `1.5` for body.
- **Tracking**: `-0.01em` on headings 14px+; `0` elsewhere; `+0.04em` uppercase on micro-caps / event tags.
- **Weights**: `400` body, `500` medium (labels, active tab), `600` headings. No 700, no 300.

## Layout primitives

- **Page shell**: centered max-width `1440px` with `24px` horizontal padding. Page title top-left, contextual controls top-right. No sidebar for this surface — audit log is a single focused view.
- **Filter bar (sticky)**: `--bg-subtle` background, `1px` bottom border, `48px` height, `12px` internal gap. Contains: campaign multi-select (popover), event-type checkboxes (popover grouped by semantic color), time-range segmented control (`15m / 1h / 6h / 24h / custom`), free-text filter over `reason` with monospace input, and a trailing result-count pill (`1,284 events`). Every filter is keyboard-addressable; `/` focuses search, `f` opens filter popover, `esc` clears.
- **Data table**: `--bg` background, `1px --border` hairlines only between rows (no vertical lines), row height `36px`, cell padding `12px` horizontal / `8px` vertical. Columns: `ts | campaign | call_id | event | reason`. `ts` and `call_id` use mono with tabular numerals and fixed width; `campaign` is a chip (`8px` radius, `--bg-hover` fill, mono label); `event` is the semantic tag (see below); `reason` is a flex column, clipped with a 2-line clamp and expandable on click. Hover: `--bg-hover` row tint, 80ms ease-out.
- **Click-to-pin strip**: sits above the table when populated — max 5 rows, dismissable per-row, persists across filter changes. Uses `shadow-md` and `6px` radius to differentiate from the live stream without feeling like a modal.
- **Empty state**: centered block, `--fg-muted` copy, small mono secondary line showing the active filter summary, and a single ghost-button "Clear filters". No illustrations, no icons.
- **Loading state**: 2px indeterminate bar in `--accent` under the filter bar; skeleton rows (muted bg, no shimmer pulse) while the first page fetches. No spinners.
- **Error state**: inline banner above the table, `--danger` left border, `--bg-subtle` fill, factual copy (`Request failed: 503 from /audit`), retry link aligned right. No toast.

## Event-type categorization

Each row carries a semantic tag in the `event` column and a `2px` left-edge color bar on hover/pin. The tag is a compact uppercase monospace chip (`11px`, `+0.04em` tracking, `4px` radius, semantic-color background at ~10% alpha with the solid accent as text color). Icons are optional and minimal — use only the Lucide set (`arrow-right`, `clock`, `alert-triangle`, `check`, `rotate-ccw`), never decorative, 12px, inline before the tag. Color is reinforcement; the tag remains authoritative.

| Event type | Semantic | Notes |
|---|---|---|
| `DISPATCH` | `--accent` | primary causal event |
| `SKIP_CONCURRENCY` / `SKIP_BUSINESS_HOUR` / `SKIP_RETRY_BACKOFF` | `--warning` | normal gating, never alarming |
| `RETRY_DUE` | `--info` | distinct violet — retry lane is visually separable from new-call lane |
| `WEBHOOK_RECEIVED` | `--fg-muted` | neutral chrome |
| `WEBHOOK_IGNORED_STALE` | `--warning` | CAS-dropped forensic marker |
| `TRANSITION` | `--fg` | neutral; `reason` payload carries the signal |
| `RECLAIM_SKIPPED_TERMINAL` | `--success` | provider confirmed terminal, no redial |
| `RECLAIM_EXECUTED` | `--danger` | epoch bump, will redial |
| `CAMPAIGN_COMPLETED` | `--success` | |

## Motion

- **Durations**: `80ms` for hover tints, `120ms` for popovers and menus, `160ms` for row expand, `200ms` cap for anything. No bounces, no springs.
- **Easing**: `cubic-bezier(0.16, 1, 0.3, 1)` (ease-out-expo) for enters; `cubic-bezier(0.4, 0, 1, 1)` for exits. One curve family across the app.
- **No**: live-tail flashes, auto-scroll pulses, pulsing dots, shimmer skeletons, confetti, AI sparkles, celebratory micro-interactions.

## Forbidden

- Gradients, glows, glassmorphism, neumorphism, multi-stop color ramps used for decoration.
- AI / brain / sparkle iconography. Generic shadcn defaults left unstyled.
- Toasts for routine events. Browser `alert` / `confirm`. Emoji in event tags.
- Donut / radial charts, oversized marketing-style charts.
- Playful error copy (`Oops!`, `All caught up!`). Errors read factually.
- Pastel badges, rounded-xl cards, soft drop shadows beyond the two tokens above.

## References

- Linear UI redesign — density + hierarchy, LCH color generation: https://linear.app/now/how-we-redesigned-the-linear-ui
- Vercel Geist — pure-neutral gray ramp, semantic accents: https://vercel.com/geist/colors
- PostHog Activity log — filter-first operator surface: https://posthog.com/docs/activity
