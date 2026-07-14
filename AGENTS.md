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
- Add or update an ADR when a change alters product scope, trust boundaries, identity, delivery guarantees, state ownership, or version policy.

## Verification

Use Python 3.12 or newer. Before handing off a change, run:

```bash
make check
```

When network access is available, also run:

```bash
make references-online
```

Review `git diff --check`, inspect the complete diff, and remove generated caches. Report checks that could not run and why.

## Repository layout

- `README.md`: human entry point and current status.
- `docs/GOAL.md`: product outcome, scope, and implementation milestones.
- `docs/architecture/README.md`: architecture index and document map.
- `docs/architecture/specification/`: normative requirements in reading order.
- `docs/adr/`: accepted architectural decisions.
- `docs/runbooks/`: operational response procedures.
- `schemas/`: machine-readable contracts.
- `examples/`: canonical valid fixtures.
- `scripts/`: local validators.
- `tests/`: regression coverage.
- `infra/bootstrap/`: planned Terraform remote-state bootstrap root.
- `infra/central/`: planned Terraform service root.
- `src/`: planned Python runtime packages.

Create planned directories only when implementation files are ready. Empty placeholders add clutter.
