---
name: okf-adopt
description: Guided first-time adoption pass for a repo with existing code and docs — inventory the knowledge, map it to source, backfill missing specs, validate. Use right after update-existing-repo on a brownfield repo, or when the owner asks to onboard/adopt an existing codebase into the kit's loop.
---

# Adoption pass

Run this once, after `bash scripts/update-existing-repo` has installed the kit's mechanics. The updater handled the deterministic part (layout detection, hooks, helper, stamp); this pass is the judgment part. Work through it with the owner — every step below ends in something they can review.

1. **Inventory the knowledge — move nothing.** Sweep the whole repo, not just the detected spec and ADR homes: spec- and ADR-shaped Markdown anywhere (frontmatter `type:` fields, `Status:` conventions, decision-record numbering), machine-readable contracts (`schemas/`), runbooks, design notes. Present the inventory: what exists, where it lives, what it appears to govern, and what looks like knowledge but isn't (meeting notes, scratch files). Ask before reclassifying anything ambiguous.

2. **Confirm the arrangement — adapt in place by default.** The kit follows the repo's own layout, naming, index format, and status conventions (the `layout:` block plus the helper's convention tolerance), so nothing has to be renamed to proceed. Offer migration to the canonical tree only as an explicit owner choice, and if they take it, you also repair everything the rename breaks: cross-links between docs, IDs referenced from code comments and tickets, the repo's own CI validators and badges, reading order. Never migrate silently or partially.

3. **Populate the map.** Fill `docs/okf-map.yml` `mappings:` so every implementation area points at its governing docs. Mine what already exists before inferring: frontmatter component lists, module boundaries, directory ownership. Mapped machine-readable contracts (schemas) count as governing docs. Once real mappings exist, `check-stale` is the authority and the crude docs-prefix stop gate retires — so this step comes early, not last.

4. **Backfill the gaps.** For implementation areas with no governing doc, run `bash scripts/okf draft <paths>`, then rewrite each draft from generated facts into commitments — what the module promises, not what its files are named. Promote into the spec home following its local conventions: `new-spec` continues a local ID sequence where one exists, and where the index is a table the helper declines and prints the entry — add it in the index's own format by hand. Map each promoted spec as you go. If the repo has its own docs validator, conform new files' frontmatter to it before committing; run that validator, don't guess at its rules.

5. **Validate end to end.** Run `bash scripts/verify-install` (from the kit clone) or its checks by hand, `bash scripts/okf check-stale`, `bash scripts/okf pending`, the repo's own docs gate and test command, and confirm the stamp file carries `kit_version`. Fix what fails; a green pass here is the handover condition.

6. **Log and hand over.** Record the pass in a dated `docs/log.md` entry: what was inventoried, mapped, promoted, and deliberately left alone — plus any helper misfire you worked around, so the friction can be harvested into the kit. If `docs/GOAL.md` is still a template, the goal interview (`okf-goal-interview` skill) is the next step; then the loop can run. A brownfield repo adopted *at* its target state is the common special case: draft the goal from the repo's own objective statement (a scope spec, the README) rather than interviewing from scratch, record an empty milestone backlog with a dated note that the success criteria pass, and leave proposing next milestones to the goal-met mechanics — don't invent backlog items to fill the template.

Scope rules: this pass authors and maps documentation — it does not refactor code, and it proposes ADRs for anything decision-shaped it uncovers rather than deciding. Existing knowledge is the owner's record: update it only where it contradicts the shipped code, and flag the contradiction rather than silently picking a side.
