---
name: okf-second-agent
description: Guided port of the installed kit's guardrails to a second agent (Codex CLI, for example) — playbook port, hook mirrors with the mirrors: declaration, adapted skills, parity guard. Use when the owner asks to add a second agent beside Claude Code, or to bring an existing ad-hoc port under the kit's conventions.
---

# Second-agent port

The kit is built for Claude Code; the goal loop stays with it. A second agent pointed at the same repo gets the same knowledge and guardrails through a port, not a copy — every dogfood repo that added one converged on the same shape. Work through it with the owner:

1. **Scope with the owner.** Which agent, and what work it takes. The dogfood precedent is commodity chores (license, CI, dependency automation, badges, metadata) with the goal loop staying in Claude Code, but the split is the owner's call. Confirm the agent's config surfaces before writing anything: playbook file, hook config, skill home.

2. **Port the playbook.** Write the second agent's playbook (`AGENTS.md` for Codex) carrying the same master objective, grounding rules, workflow, and guardrails as `CLAUDE.md`. Two mechanics do not port: `@` imports are Claude Code-only, so the ported playbook lists the same files (`docs/GOAL.md` and the spec/ADR indexes) as explicit read-at-session-start instructions; and the env-file read denial in `.claude/settings.json` is enforced only by Claude Code, so the ported secrets guardrail states honestly that for this agent it is policy prose, not a mechanical gate. Never weaken a guardrail in the port — restate it for the new agent.

3. **Mirror the hooks and declare the mirror.** Copy the two hook scripts byte-identical from `.claude/hooks/` into the agent's hook directory (`.codex/hooks/`, for example) and wire the agent's own hook config to run them — the shipped scripts resolve their root via `CLAUDE_PROJECT_DIR`, then `CODEX_PROJECT_DIR`, then the current directory, so unmodified copies work. Then declare the mirror directory in a top-level `mirrors:` list in `docs/okf-map.yml` (for example `- .codex/hooks`): declared mirrors are synced by the safe updater on every kit upgrade through the provenance path (ADR 0021), while undeclared ones stay yours to re-sync by hand and draw a session-start advisory. Never edit a mirror to "adapt" it — mirrors stay byte-identical, and per-agent behavior belongs in the agent's own config.

4. **Adapt the skills (optional, owner-managed).** If the second agent loads skills, copy the `okf-*` set into its skill home with the per-agent substitutions it needs (file names, command phrasing). The updater never syncs mirrored skills — the kit cannot reproduce legitimate per-agent adaptations — so keep the sets paired by hand: when a kit upgrade refreshes a `.claude/skills/` file, carry the change over.

5. **Guard parity in the repo's own gate.** Add a check to this repo's tests or validation (mirrored hooks byte-identical to `.claude/hooks/`, skill sets paired, skipped entirely when the second stack is absent). The updater and `verify-install` cover declared mirrors; the repo-side check is belt-and-suspenders for hand edits and anything undeclared.

6. **Commit and log.** Commit the second agent's config — the shipped hooks already treat those paths as agent config, so they won't re-trigger the docs-sync block. Add a dated `docs/log.md` entry naming what was ported, which guardrails are policy-only for the new agent, and the declared mirror directories. Run `bash scripts/okf check-stale` and the repo's own gate before finishing.
