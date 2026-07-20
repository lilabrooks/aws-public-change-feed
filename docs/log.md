# Log

## 2026-07-19

- Adopted the claude-okf-repo-kit (0.3.5) through its brownfield path:
  `update-existing-repo` detected the layout (specs at
  `docs/architecture/specification/`, recorded in the new
  `docs/okf-map.yml` layout block), left the three-line `CLAUDE.md` import
  shim untouched and staged the kit playbook as `AGENTS.2.md` beside our
  `AGENTS.md`, seeded `docs/architecture/specification/index.md` and
  `docs/adr/index.md` from the existing chapters and ADRs (the fourteen
  active ADRs; `docs/adr/archive/` is outside the scan), and created the
  bundle root `docs/index.md` (stamped `okf_version` 0.1, `kit_version`
  0.3.5, linking runbooks and schemas) plus this log. Installed mechanics:
  `.claude/settings.json` (hooks plus the `.env` read denial),
  the Stop docs-sync and SessionStart version hooks, `scripts/okf`, and the
  six `okf-*` skills. Adoption-pass judgment on top: populated the map —
  `schemas/**`, `examples/**`, and `scripts/validate_config.py` map to
  chapter 03 plus ADR-011, `tests/**` maps to chapter 06; `site/` and
  `scripts/validate_site.py` deliberately unmapped because that sync is
  already guarded mechanically by the validator and the quality workflow;
  commented future mappings for the planned `src/` and `infra/` roots.
  Merged the kit mechanics into `AGENTS.md` in this repo's own voice
  (log discipline, `check-stale` step, proposed-ADR scaffolding via
  `okf new-adr`, a Kit mechanics section, layout entries) and added
  `@docs/adr/index.md` to the `CLAUDE.md` shim; then discarded the
  `AGENTS.2.md` and `docs/GOAL.2.md` candidates as reviewed. The goal
  candidate was a false positive worth harvesting: `docs/GOAL.md` is filled,
  but its `# Goal: <title>` heading and `## Implementation milestones`
  section defeat the kit's exact `# Goal`/`# Milestones` filled-goal
  detection. No knowledge was moved or renamed; `docs/architecture/README.md`
  remains the human reading order beside the kit's seeded index. Verified:
  `make check` green (84 tests plus config/reference/site validators),
  `bash scripts/okf check-stale` current, `bash scripts/okf pending` empty,
  kit `verify-install` passed with zero warnings, and the SessionStart hook
  is silent. Environment-only: a local `.venv` (git-ignored) now carries
  `requirements-dev.txt` for the gate.
- Follow-up from the adoption PR's CI: the quality workflow's site-sync gate
  (`validate_site.py --base --head`) correctly flagged that the new
  `docs/architecture/specification/index.md` and `docs/adr/index.md` landed
  without a `site/index.html` co-change — the local gate ran on a clean tree
  and could not see the branch diff. Resolved with an accurate one-sentence
  addition to the page's authoritative-source section describing the
  source-to-knowledge map, staleness checks, and dated change log the
  adoption introduced.
