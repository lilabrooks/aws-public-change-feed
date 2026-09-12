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

ADR-027 is implemented in source by 35-day PITR on both central DynamoDB
tables, one stopped-runtime restored-pair binding, a scoped recovery role, and
the digest-bound recovery proof command. ADR-028 adds a separate CloudTrail
evidence role and canonical event evidence for active tables that omit their
restore summary. The 2026-09-05 dev proof completed restore verification,
disabled-trigger cutover, rollback, trigger restoration, and exact cleanup. It
met the nominal recovery objectives and left the primary runtime healthy. L-41
is complete. Independent recovery digest vectors are now covered by tests, and
the central source enables deletion protection on both primary tables while
leaving preflight tables disposable. The exact L-50 plan changed only those two
tables in place; owner-authorized apply, direct enabled readback, and a no-change
central plan completed on 2026-09-05. The later M3 gate accepted that bounded
recovery claim; it does not claim service operation on restored tables.

Accepted ADR-029 defines L-52's package-provenance repair. It keeps volatile
build attestations outside package identity, adds a stable in-archive input
manifest, requires source-member and handler checks before publication, uses an
S3-computed SHA-256 for checksum-bearing objects, and adds a Linux target import
gate. Accepted ADR-026 and its L-53 revision define the one exact legacy-package
exception, checksum enforcement for every other selection, and full pause of
all four durable executors. The L-54 application-only exercise passed the exact
five-function rollback and forward restoration, stopped-state comparison,
normal scheduled observation, and final Terraform convergence. Its [public
record](../evidence/l54-application-rollback-2026-09-07.md) binds that result.
Final candidate `91f8db0e9b1f6cd6a1889face54589feaef295c0`
passed every required check in PR #196. The repository owner accepted L-43's
terminal `passed` disposition on 2026-09-07. M3 is production-ready for the
exact one-environment, one-destination, four-feed, three-service, four-rule dev
deployment and its 300-delivery/hour envelope, subject to the limits retained
in the [readiness assessment](../evidence/m3-production-readiness-assessment-2026-09-06.md).
[How readiness was established](../production-readiness.md) explains the decision and links its supporting records.

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
- [ADR-025: Source-state and response-page retirement](../adr/025-source-state-and-response-page-retirement.md)
- [ADR-026: Central shadow and rollback proof](../adr/026-central-shadow-and-rollback-proof.md)
- [ADR-027: DynamoDB point-in-time recovery for both state tables](../adr/027-dynamodb-point-in-time-recovery.md)
- [ADR-028: Separate CloudTrail evidence for DynamoDB restore identity](../adr/028-separate-cloudtrail-evidence-for-dynamodb-restore-identity.md)
- [ADR-029: Lambda package provenance and entrypoint proof](../adr/029-lambda-package-provenance-and-entrypoint-proof.md)
- [ADR-030: Bounded live windows and Terraform parking](../adr/030-bounded-live-windows-and-terraform-parking.md)

The accepted [live-window operator workflow](../runbooks/live-window.md) bundles
bounded activation, live testing, and Terraform parking. It preserves retained
data while removing monitoring and fencing runtime execution between tests.
Failure-only notifications remain available while parked. Control artifacts
use verified closeouts and separate, hash-approved retirement after 90 days;
application retention is unchanged.
The owner accepted ADR-030 on 2026-09-07 and its supervised-use qualification
revision on 2026-09-08.
Initial parked migration and control-plane deployment completed on 2026-09-07,
with post-wait control verification, AWS definition validation, and no-change
plans. The [runbook deployment record](../runbooks/live-window.md#initial-deployment-record-2026-09-07)
states the exact evidence boundary. The later supervised attempt proved
parking-only recovery, tagged baseline-alarm recreation/removal, and parked
convergence under the build role. Build and terminal-failure emails were
received, though the terminal receipt did not identify its route.
The later attempt exposed the tagged EventBridge rule-update permission gap;
failure cleanup passed. The constrained repair was applied through a separately
reviewed parked IAM plan, with matching policy readback and no control-plan
changes afterward. The subsequent supervised unpark/early-park passed with
live readiness, successful rule/mapping updates in both directions, unchanged
identities and rule tags, absent parked monitoring, and no-change central/control
plans. Eligible evidence closeout passed without pruning objects.
The later [L-57/deadline proof](../evidence/l57-l58-supervised-live-window-2026-09-08.md)
passed the fixed 20-minute scheduled observation and automatic parking after
local waiter loss, with the same owner and deadline and no early-stop request.
The lifecycle is qualified for supervised short dev windows; an operator stays
available through terminal results, independent parked verification, and
recovery if needed. GitHub issues and milestones hold M4's closure state.
[L-59](https://github.com/lilabrooks/aws-public-change-feed/issues/205) holds
route-specific receipt and live failed-publication qualification before
unattended use, outside M4. Those checks remain unperformed, not passed.
Fresh per-use checks and changed-mechanism requalification still apply.

The [resource-tag policy](specification/05-security-and-operations.md#resource-ownership-and-cost-tags)
groups supported Terraform resources by project, deployment, management, and
component. Static tags cover retained infrastructure and recreated alarms;
provider exceptions are schema-checked. Tagging maintenance and billing
activation are separate from park/unpark and do not establish shutdown or
complete cost attribution.

The [September 7 tag deployment record](../runbooks/live-window.md#tag-deployment-record-2026-09-07)
confirms the separate maintenance apply and direct tag readback for 47 existing
resources. All three full Terraform plans converged with no changes, and dev
remained parked. Billing reports `project` and `deployment_id` Active;
`component` discovery and cost attribution remain pending.

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
| Lambda package provenance | [`lambda-package-manifest.schema.json`](../../schemas/lambda-package-manifest.schema.json) | [`lambda-package-manifest.json`](../../examples/lambda-package-manifest.json) |

The Lambda package manifest example uses fixed synthetic package inputs to show
the strict document shape; an independent known answer pins its canonical
encoding. Package tests recompute the manifest for the current real source tree
and bind the reviewed builder, lock, handler, and target identities separately,
so an ordinary source edit does not turn the public example into a volatile
build record.

[`validate_config.py`](../../scripts/validate_config.py) enforces schema and cross-document rules. [`test_validate_config.py`](../../tests/test_validate_config.py) keeps a regression case for each rejected mutation.

The six release and delivery files under [`examples/`](../../examples/) form one
executable contract bundle. The Lambda package manifest is the separate
illustrative example described above. The validator loads the six bundle files
together, checks each against its paired contract, and then verifies their
shared projections, references, release hashes, deterministic identities,
routes, retention rules, and size limits. This proves both individual file
shape and cross-file behavior.

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

`infra/bootstrap/` provisions the versioned remote-state bucket, and `infra/central/` decodes the deployment input and provisions the config bucket, source-state and delivery tables, FIFO queue and delivery DLQ, Slack credential containers, operational topic, runtime-failure queue, the chapter 05 IAM roles, and the log groups, dashboard, and alarms. It conditionally defines the watcher, read-only shadow evaluator, regular dispatcher, Slack worker, and recovery reconciler Lambdas from exact package digests and S3 versions. The watcher runs every 15 minutes, persists exact raw snapshots and conditional DynamoDB source state, and holds feed validators until durable candidate evidence is read back. The direct-invocation shadow evaluator reuses the watcher's package and feed policy but evaluates through fresh in-memory stores under a role with no durable-state or external-side-effect authority. Its separate operator role can invoke only that function, and asynchronous invocations have zero retries. The dispatcher runs every minute and moves exact due requests through the FIFO queue; the worker consumes FIFO batches; the reconciler runs every five minutes with bounded observations and repairs. `src/` holds the five runtime composition roots, recovery and worker cores, watcher orchestration, and their AWS adapters. [ADR-020](../adr/020-exact-application-version-gate-for-delivery.md) is implemented by a deterministic package builder, an append-only digest-key publisher, exact S3 object-version inputs, and one shared `sha256:` application value for the watcher, dispatcher, and worker. [ADR-022](../adr/022-preview-first-application-package-retirement.md) adds a bounded preview/apply tool and a separate exact-prefix retirement role without widening publisher or runtime authority. The release publisher also owns separate digest-bound configuration-release retirement and preview-bound retained-pointer rollback commands. Accepted [ADR-026](../adr/026-central-shadow-and-rollback-proof.md) records the central L-42 boundary, including the watcher execution pause and the five-function application rollback. [ADR-007](../adr/007-central-slack-delivery-queue-and-worker.md) owns FIFO ordering, the worker capacity boundary, and the recovery state matrix. Release publication has an S3 adapter already, because [ADR-019](../adr/019-s3-preconditions-for-release-publication-and-promotion.md) makes the store's error codes part of the contract rather than a deployment detail. Keep a concept in one owning document and link to it elsewhere. Do not copy full requirements between the goal, specification, ADRs, and runbook.

`infra/preflight/` reuses the central resource module under the separate
ADR-024 state key. It fixes mutable identities to the isolated exercise
deployment, keeps the persistent application object read-only, and gives the
recovery, fixed-load, and exact-teardown protocols their own preview-first
runner.

Accepted [ADR-029](../adr/029-lambda-package-provenance-and-entrypoint-proof.md)
binds package-producing inputs through a stable archive manifest, refuses
unsafe or source-divergent ZIPs before publication, and checks real imports on
the Linux target. It defers signed hosted-build provenance until the deployment
has another publisher, CI publication, an external consumer, or a production
policy that needs cryptographic build origin.

## Public page maintenance

[`site/index.html`](../../site/index.html) is the concise public explanation of this architecture. The editable diagram lives in [`site/architecture.drawio`](../../site/architecture.drawio), and the page renders its committed [`site/architecture.svg`](../../site/architecture.svg) export without a client-side diagram runtime.

The [service walkthrough](../evidence/mvp-walkthrough.md) explains public-feed matching, the Lambda workflow, state ownership, and demonstrated delivery, recovery, load, and rollback results in ten scenes. It also covers M4’s supervised operation and M5’s four offline maintenance improvements. Its poster reads “Architecture, delivery, and operations” without milestone badges. Current video, captions, chapter markers, poster, editable SVG frames, and hashes live under [`site/media/walkthrough-v4/`](../../site/media/walkthrough-v4/). The original MVP media and slide deck remain under [`site/media/mvp-evidence-v2/`](../../site/media/mvp-evidence-v2/); their recorded hashes are unchanged.

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
