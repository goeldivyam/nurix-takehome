---
name: update-docs
description: Review recent code and design changes and update all project documentation to keep it accurate. Run after significant changes or before submission.
disable-model-invocation: true
---

# Update Documentation

Review recent changes and update all relevant documentation so `CLAUDE.md`, skills, and the README stay accurate. This project is a Nurix.ai take-home — documentation must be reviewer-ready at all times, not just at submission.

## Steps

1. **Check what changed** — run `git diff HEAD~5 --stat` (or use `$ARGUMENTS` for a specific range) to see recently modified files. Also run `git status` to see uncommitted work.

2. **Update `CLAUDE.md`** if:
   - Any rule, non-negotiable, or anti-pattern changed
   - Workflow steps changed
   - The agent dispatch table changed (new agent added, or trigger conditions changed)
   - The tech stack or architecture-at-a-glance changed
   - File: `CLAUDE.md`

3. **Update `architecture-overview` skill** if:
   - A new module / package / service was added or renamed
   - A new API endpoint or route was added
   - A new table, column, or index was added to the schema
   - A new shared component or cross-cutting pattern was introduced
   - File: `.claude/skills/architecture-overview/SKILL.md` (create if missing)

4. **Update `code-quality` skill** if:
   - A new naming convention was adopted
   - A new domain term was introduced (e.g., `deficit`, `quantum`, `cps_budget`)
   - A new anti-pattern was added to the "do not do this" list
   - File: `.claude/skills/code-quality/SKILL.md` (create if missing)

5. **Update `backend-conventions` skill** if:
   - Database / query patterns changed (new JOIN pattern, new index strategy)
   - Async / concurrency patterns changed
   - The telephony provider abstraction's contract changed
   - The scheduler's public surface changed
   - File: `.claude/skills/backend-conventions/SKILL.md` (create if missing)

6. **Update the schema file** if the DB was modified:
   - Ensure the canonical schema file (e.g., `schema.sql` or `backend/schema.sql`) matches the current DB state
   - ONE schema file is the single source of truth — no separate migration files

7. **Update `README.md`** if:
   - Setup steps changed (Docker Compose commands, dependency installs)
   - Example `curl` / API usage changed
   - The system-design summary changed (scheduler policy, state model, retry behavior)
   - The fault-tolerance or scaling story changed
   - The file tree changed (new top-level dir, renamed module)
   - File: `README.md`

8. **Update any design docs under `docs/` or `context/`** if:
   - Architecture diagrams changed
   - State-machine or scheduler-policy explanations changed
   - Trade-off rationales changed (e.g., DRR vs WFQ decision was revised)

9. **Agent files** — agents are kept in sync manually, not via this skill. If rubric ground truth changes (e.g., Mohit adds new evaluation criteria), note it in `CLAUDE.md` and ask whether to propagate edits into the affected agent files.

10. **Report** what was updated and what was already accurate. Flag any documentation that is still stale but was not updated — do not silently skip.

## Rules

- Never create a documentation file that isn't referenced from here or `CLAUDE.md`. Dead docs rot faster than dead code.
- Prefer updating an existing file over creating a new one.
- If two docs would say the same thing, consolidate — single source of truth per topic.
- Keep each doc tight. This is a take-home reviewer's environment — they read, they don't skim.
