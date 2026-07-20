# AGENTS.md

## Mission

Build and maintain AWS Public Change Alerting as a public-feed intelligence service. The product ingests approved public AWS feeds, produces explainable route-scoped candidates, and uses Slack as its delivery channel.

## Read order

1. `docs/GOAL.md`
2. `docs/architecture/README.md`
3. `docs/architecture/specification/01-overview.md` through `06-acceptance-and-generation.md`
4. Applicable accepted ADRs in `docs/adr/`
5. Schemas, examples, validators, and tests

If documents disagree, accepted ADRs govern decisions, numbered specifications govern required behavior, schemas govern file shape, and semantic validators govern cross-file invariants. Update every affected layer together.

## Scope guardrails

Keep work inside the boundary set by ADR-017:

- Public AWS RSS and Atom acquisition.
- Deterministic service and risk matching.
- Static environment/profile/customer/route mapping.
- Candidate history, durable outbox, retries, dedupe, and Slack delivery.

Do not add customer-account access, account telemetry, cost collection, security-finding ingestion, remediation, ticketing, incident workflows, or generic external adapters without a new accepted scope decision.

Use “potentially relevant” for environment matches. Public announcements do not prove customer impact.

## Change discipline

- Preserve deterministic identity algorithms and null-framed hashing unless a contract version changes.
- Reject unknown fields in owned schemas.
- Add a regression test for every rejected configuration or contract mutation.
- Keep examples valid and recalculate release, candidate, and request hashes after relevant fixture edits.
- Keep runtime credentials out of configuration, inventory, candidates, logs, and fixtures.
- Use immutable configuration releases and exact object versions.
- Treat DynamoDB as the delivery system of record and SQS as transport.
- Preserve the explicit `delivery_unknown` state. Never claim exactly-once Slack delivery.
- Add or update an ADR when a change alters product scope, trust boundaries, identity, delivery guarantees, state ownership, or version policy. Scaffold with `bash scripts/okf new-adr <slug> "Title"` (it continues the three-digit numbering) and mark new decisions `- Status: Proposed` for the owner's review; `bash scripts/okf pending` lists what awaits it.
- Record every meaningful change as a dated entry in `docs/log.md`, newest first. The Stop hook blocks a session from ending with code changes and no docs update or log rationale.
- After changing mapped source areas, run `bash scripts/okf check-stale`; update the governing document it names or add the `docs/log.md` rationale. `docs/okf-map.yml` maps source areas to the specifications, ADRs, and schemas that govern them.

## Verification

Use Python 3.12 or newer. Before handing off a change, run:

```bash
make check
```

When network access is available, also run:

```bash
make references-online
```

Also run `bash scripts/okf check-stale` and resolve anything it reports.

Review `git diff --check`, inspect the complete diff, and remove generated caches. Report checks that could not run and why.

## Kit mechanics (claude-okf-repo-kit)

The repo carries the kit's guardrails; the layout block in `docs/okf-map.yml` points every kit tool at this repo's own arrangement (`docs/architecture/specification/`, three-digit ADRs).

- `.claude/hooks/check-docs-sync.sh` (Stop) and `.claude/hooks/check-okf-version.sh` (SessionStart) are committed guardrails — do not move, rename, or disable them; if the Stop hook blocks, make the docs or log update it asks for.
- `scripts/okf` is the deterministic path for numbering, indexes, and staleness (`check-stale`, `draft`, `adr-suggest`, `new-adr`, `new-spec`, `pending`). Prefer it over re-deriving mechanics; if it declines on this repo's conventions, do the workaround in the open and log it.
- `docs/index.md` is the bundle root: its `kit_version` stamp drives the SessionStart drift note. On drift, recommend the safe updater (`scripts/update-existing-repo` from an up-to-date kit clone; the `okf-kit-upgrade` skill carries the walkthrough). The same hook reports numbered kit candidates (`AGENTS.2.md` and similar) left unresolved — merge what the owner wants and delete them; never commit one unresolved.
- The `okf-*` skills under `.claude/skills/` carry the episodic procedures (goal interview, acceptance pass, ADR review, kit upgrade, adoption pass, second-agent port). If a skill doesn't load, the rules here still bind.
- Keep spec and ADR index files (`docs/architecture/specification/index.md`, `docs/adr/index.md`) current when files are added or renamed; `docs/architecture/README.md` remains the human reading order.

## Repository layout

- `README.md`: human entry point and current status.
- `docs/GOAL.md`: product outcome, scope, and implementation milestones.
- `docs/architecture/README.md`: architecture index and document map.
- `docs/architecture/specification/`: normative requirements in reading order.
- `docs/adr/`: accepted architectural decisions.
- `docs/index.md`: knowledge-bundle root with the `okf_version`/`kit_version` stamps.
- `docs/log.md`: dated change log, newest first.
- `docs/okf-map.yml`: source-to-knowledge map (with the kit layout block).
- `docs/runbooks/`: operational response procedures.
- `schemas/`: machine-readable contracts.
- `examples/`: canonical valid fixtures.
- `scripts/`: local validators.
- `tests/`: regression coverage.
- `infra/bootstrap/`: planned Terraform remote-state bootstrap root.
- `infra/central/`: planned Terraform service root.
- `src/`: planned Python runtime packages.

Create planned directories only when implementation files are ready. Empty placeholders add clutter.
