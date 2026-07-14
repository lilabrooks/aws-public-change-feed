# Architecture index

This page is the map for the files that define AWS Public Change Alerting. Read the numbered specification in order. Use ADRs for the reason behind settled choices and the runbook for operational response.

The [public architecture page](https://lilabrooks.github.io/aws-public-change-feed/) presents the value proposition, decision rationale, processing flow, and current evidence. This index and the numbered specification remain authoritative when the public explanation and normative requirements differ.

## Normative specification

1. [Overview and product boundary](specification/01-overview.md)
2. [Platform and state model](specification/02-platform.md)
3. [Configuration and release model](specification/03-configuration.md)
4. [Feed processing and delivery](specification/04-alert-processing.md)
5. [Security and operations](specification/05-security-and-operations.md)
6. [Acceptance and implementation sequence](specification/06-acceptance-and-generation.md)

The [goal](../GOAL.md) defines the outcome and milestones. The [operations runbook](../runbooks/operations.md) defines response procedures.

## Architecture decision records

- [ADR-001: Separate deployment and runtime configuration](../adr/001-separate-deployment-and-runtime-configuration.md)
- [ADR-002: Route-scoped candidates and delivery identity](../adr/002-route-scoped-slack-delivery-and-dedupe.md)
- [ADR-004: Explicit Slack delivery guarantees](../adr/004-explicit-slack-delivery-guarantees.md)
- [ADR-006: Terraform and Python implementation baseline](../adr/006-terraform-and-python-implementation-baseline.md)
- [ADR-007: Durable outbox and Slack worker](../adr/007-central-slack-delivery-queue-and-worker.md)
- [ADR-009: Feed acquisition and deterministic matching](../adr/009-feed-acquisition-and-deterministic-matching.md)
- [ADR-010: Operations and supported scale](../adr/010-operations-and-supported-scale.md)
- [ADR-011: Machine-readable configuration contracts](../adr/011-machine-readable-configuration-contracts.md)
- [ADR-013: Feed state and announcement identity](../adr/013-source-state-and-public-announcement-identity.md)
- [ADR-014: Immutable release artifacts and retention](../adr/014-immutable-release-artifacts-and-retention.md)
- [ADR-015: Slack rendering, rate control, and retry](../adr/015-slack-rendering-rate-control-and-retry.md)
- [ADR-016: Production preflight and event contracts](../adr/016-production-preflight-and-event-contracts.md)
- [ADR-017: Public-feed-only product scope](../adr/017-public-feed-only-product-scope.md)

ADR numbers 003, 005, 008, and 012 were superseded when ADR-017 narrowed the product. [Archived copies](../adr/archive/README.md) preserve them for audit, separate from the accepted decisions that govern the current product. Numbering remains stable so earlier links and review notes are auditable.

## Machine-readable architecture

| Concern | Contract | Canonical example |
| --- | --- | --- |
| Infrastructure inputs | [`deployment.schema.json`](../../schemas/deployment.schema.json) | [`deployment.yaml`](../../examples/deployment.yaml) |
| Feed and matching policy | [`config.schema.json`](../../schemas/config.schema.json) | [`config.yaml`](../../examples/config.yaml) |
| Runtime environment projection | [`inventory.schema.json`](../../schemas/inventory.schema.json) | [`inventory.json`](../../examples/inventory.json) |
| Active immutable release | [`active-versions.schema.json`](../../schemas/active-versions.schema.json) | [`active-versions.json`](../../examples/active-versions.json) |
| Feed output | [`alert-candidate.schema.json`](../../schemas/alert-candidate.schema.json) | [`alert-candidate.json`](../../examples/alert-candidate.json) |
| Slack work item | [`delivery-request.schema.json`](../../schemas/delivery-request.schema.json) | [`delivery-request.json`](../../examples/delivery-request.json) |

[`validate_config.py`](../../scripts/validate_config.py) enforces schema and cross-document rules. [`test_validate_config.py`](../../tests/test_validate_config.py) keeps a regression case for each rejected mutation.

The six files under [`examples/`](../../examples/) form one executable contract bundle. The validator loads them together, checks each file against the contract in the same table row, and then verifies their shared projections, references, release hashes, deterministic identities, routes, retention rules, and size limits. This proves both individual file shape and cross-file behavior.

Tests create mutations from this canonical valid bundle and confirm that each invalid change is rejected. A contract edit updates every affected schema, example, semantic validator, and regression test in the same change. Edits that affect release, candidate, or request identity also recalculate every dependent hash.

## Folder structure

```text
.
├── README.md                    Human entry point and project status
├── AGENTS.md                    Agent working rules and read order
├── docs/
│   ├── GOAL.md                  Outcome, scope, and milestones
│   ├── architecture/
│   │   ├── README.md            This index
│   │   └── specification/       Normative requirements, in order
│   ├── adr/                     Accepted decisions and superseded archive
│   └── runbooks/                Operational procedures
├── schemas/                     JSON Schema contracts
├── examples/                    Canonical executable contract fixtures
├── site/                        GitHub Pages source and Mermaid processing flow
├── scripts/                     Repository validators
├── tests/                       Regression tests
├── Makefile                     Local quality entry points
└── requirements-dev.txt         Pinned validation dependencies
```

The planned implementation adds `infra/bootstrap/`, `infra/central/`, and `src/` when those directories contain working files. Keep a concept in one owning document and link to it elsewhere. Do not copy full requirements between the goal, specification, ADRs, and runbook.

## Public page maintenance

[`site/index.html`](../../site/index.html) is the concise public explanation of this architecture. Its Mermaid source is committed separately in [`site/architecture.mmd`](../../site/architecture.mmd) and must match the copy embedded in the page.

[`validate_site.py`](../../scripts/validate_site.py) checks page structure, local assets, the pinned Mermaid runtime, diagram parity, and the README link. During pull requests, the repository quality workflow also requires `site/index.html` to change whenever the goal, architecture, ADRs, schemas, examples, Mermaid source, or supporting site assets change. This makes public-page review part of every change that can alter its claims.

References verified: 2026-07-13.

## Reference maintenance

Markdown containing external URLs includes a dated `References verified` marker. Local validation checks marker age, local links, anchors, and documented Lychee exclusions. `make references-online` performs the network-backed link check. A future exclusion in `.lycheeignore` needs a reason and expiry directly above its pattern.
