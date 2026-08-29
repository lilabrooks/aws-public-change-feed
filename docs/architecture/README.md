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
- [ADR-018: Corpus evaluation and matching thresholds](../adr/018-corpus-evaluation-and-matching-thresholds.md)
- [ADR-019: S3 preconditions for release publication and promotion](../adr/019-s3-preconditions-for-release-publication-and-promotion.md)
- [ADR-020: Exact application-version gate for delivery](../adr/020-exact-application-version-gate-for-delivery.md)
- [ADR-021: Audited replay of exact terminal delivery records](../adr/021-audited-terminal-record-replay.md)
- [ADR-022: Preview-first application package retirement](../adr/022-preview-first-application-package-retirement.md)
- [ADR-023: Scoped active-manifest absence detection](../adr/023-scoped-active-manifest-absence-detection.md)
- [ADR-024: Isolated live runtime exercises](../adr/024-isolated-live-runtime-exercises.md)

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
| Labeled matching corpus | [`corpus.schema.json`](../../schemas/corpus.schema.json) | [`announcements.json`](../../corpus/announcements.json) |
| Approved matching thresholds | [`corpus-thresholds.schema.json`](../../schemas/corpus-thresholds.schema.json) | [`thresholds.json`](../../corpus/thresholds.json) |

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
│   ├── adr/                     Decision records and superseded archive
│   └── runbooks/                Operational procedures
├── infra/                       Terraform roots (bootstrap, central, and isolated preflight built)
├── schemas/                     JSON Schema contracts
├── config/                      Reviewed environment policy inputs
├── examples/                    Canonical executable contract fixtures
├── corpus/                      Labeled announcements and approved thresholds
├── src/                         Python runtime packages
├── site/                        GitHub Pages source, draw.io diagram, and SVG export
├── scripts/                     Repository validators
├── tests/                       Regression tests
├── Makefile                     Local quality entry points
└── requirements-dev.txt         Pinned validation dependencies
```

`infra/bootstrap/` provisions the versioned remote-state bucket, and `infra/central/` decodes the deployment input and provisions the config bucket, source-state and delivery tables, FIFO queue and delivery DLQ, Slack credential containers, operational topic, runtime-failure queue, the chapter 05 IAM roles, and the log groups, dashboard, and alarms. It conditionally defines the watcher, regular dispatcher, Slack worker, and recovery reconciler Lambdas from exact package digests and S3 versions. The watcher runs every 15 minutes, persists exact raw snapshots and conditional DynamoDB source state, and holds feed validators until durable candidate evidence is read back. The dispatcher runs every minute and moves exact due requests through the FIFO queue; the worker consumes FIFO batches; the reconciler runs every five minutes with bounded observations and repairs. `src/` holds the four runtime composition roots, recovery and worker cores, watcher orchestration, and their AWS adapters. [ADR-020](../adr/020-exact-application-version-gate-for-delivery.md) is implemented by a deterministic package builder, an append-only digest-key publisher, exact S3 object-version inputs, and one shared `sha256:` application value for the watcher, dispatcher, and worker. [ADR-022](../adr/022-preview-first-application-package-retirement.md) adds a bounded preview/apply tool and a separate exact-prefix retirement role without widening publisher or runtime authority. [ADR-007](../adr/007-central-slack-delivery-queue-and-worker.md) owns FIFO ordering, the worker capacity boundary, and the recovery state matrix. Release publication has an S3 adapter already, because [ADR-019](../adr/019-s3-preconditions-for-release-publication-and-promotion.md) makes the store's error codes part of the contract rather than a deployment detail. Keep a concept in one owning document and link to it elsewhere. Do not copy full requirements between the goal, specification, ADRs, and runbook.

`infra/preflight/` reuses the central resource module under the separate
ADR-024 state key. It fixes mutable identities to the isolated exercise
deployment, keeps the persistent application object read-only, and gives the
recovery, fixed-load, and exact-teardown protocols their own preview-first
runner.

## Public page maintenance

[`site/index.html`](../../site/index.html) is the concise public explanation of this architecture. The editable diagram lives in [`site/architecture.drawio`](../../site/architecture.drawio), and the page renders its committed [`site/architecture.svg`](../../site/architecture.svg) export without a client-side diagram runtime.

The dev MVP walkthrough is indexed by [`docs/evidence/mvp-walkthrough.md`](../evidence/mvp-walkthrough.md). Its committed public assets live under [`site/media/mvp-evidence-v2/`](../../site/media/mvp-evidence-v2/): the 720p web video, slide deck, PDF, poster, WebVTT captions, and SHA-256 manifest. The 1080p MP4 is the `mvp-evidence-v2` GitHub Release asset named by the page and manifest, which keeps the largest binary out of Git history.

The draw.io source uses AWS4 resource-icon cells for AWS services. The committed SVG embeds the matching service artwork from AWS's 2026-07-31 Architecture Icons package. Use the [official AWS architecture icons page](https://aws.amazon.com/architecture/icons/) when that artwork needs to be refreshed.

To revise the diagram:

1. Open `site/architecture.drawio` in draw.io and edit the existing page. Keep the stable cell IDs on the required processing nodes and edges.
2. Export that page as SVG to `site/architecture.svg`. Keep the full diagram bounds and avoid external image references.
3. Run `python scripts/stamp_drawio_export.py`. This records the exact `.drawio` SHA-256 on the SVG and restores the accessible SVG metadata when the exporter omits it.
4. Run `python scripts/validate_site.py`, then inspect the page at desktop and narrow widths.

[`validate_site.py`](../../scripts/validate_site.py) checks page structure, local assets, MVP media hashes and captions, required draw.io nodes and edges, the SVG source hash and accessibility metadata, and the README link. During pull requests, the repository quality workflow also requires `site/index.html` to change whenever the goal, architecture, ADRs, schemas, examples, draw.io source, SVG export, or supporting site assets change. This makes public-page review part of every change that can alter its claims.

References verified: 2026-07-13.

## Reference maintenance

Markdown containing external URLs includes a dated `References verified` marker. Local validation checks marker age, local links, anchors, and documented Lychee exclusions. `make references-online` performs the network-backed link check. A future exclusion in `.lycheeignore` needs a reason and expiry directly above its pattern.
