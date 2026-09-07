# Goal: AWS Public Change Alerting

## Outcome

Deliver an AWS-hosted service that matches approved public AWS announcements against configured services and risk phrases, records the matched text and mapped audience in route-scoped review candidates, and delivers those candidates to the correct Slack destinations.

The service helps a team operating repeated AWS stacks answer a narrow question: which public AWS changes deserve review for the services declared in each environment profile? It infers possible relevance from configuration and preserves the announcement title, summary, URL, matched terms, and release. It does not assert confirmed account or resource impact.

## Product value

The useful output is more specific than a general AWS news feed:

- Every item names the matched service, risk type, rule, source, and exact announcement revision.
- Static environment profiles identify where review may be needed without customer-account permissions.
- Route-scoped candidates keep customer and team destinations isolated.
- Immutable releases make each decision reproducible.
- Durable outbox and delivery state make missed, duplicated, delayed, and ambiguous Slack work visible.

Slack carries the generated feed. It is not the source of truth for candidates or delivery state.

## Deliverables

- Safe RSS and Atom acquisition with per-feed validators, freshness, raw snapshot retention, and replay support.
- Announcement normalization, canonical identity, revision tracking, and provenance merging.
- A versioned service catalog, stack profiles, environment policy, and deterministic risk-rule DSL.
- Route-scoped `AlertCandidate` version 3 and `DeliveryRequest` version 3 contracts.
- An immutable `config.yaml` and `inventory.json` release process.
- DynamoDB feed, announcement, candidate, destination, and delivery state.
- An encrypted SQS FIFO queue, dispatcher, Slack worker, DLQ, and recovery reconciler.
- Incoming-webhook and bot-token delivery modes with destination pacing and explicit unknown outcomes.
- A rendered sample of the canonical candidate on the public architecture page, produced by the delivery renderer rather than hand-written. This documents the message shape; it is not a second delivery path, and ADR-017 keeps Slack the only one.
- Terraform bootstrap and service roots, least-privilege IAM, schedules, dashboards, alarms, and operational runbooks.
- Historical-corpus, unit, contract, integration, recovery, load, and production preflight tests.

## Scope exclusions

- Customer-account API access, role assumption, resource discovery, or telemetry.
- Account-specific event and security-finding ingestion.
- Spend collection or financial analysis.
- Remediation, change execution, ticketing, incident ownership, or Slack acknowledgement workflows.
- External platform adapters or configurable handoff protocols.
- Proof that a public announcement affects a particular account or resource.
- Exactly-once Slack delivery.

## Quality bar

- Python requires version 3.12 or newer.
- Terraform requires `>= 1.10.0, < 2.0.0` until a 2.x release is qualified.
- Configuration, inventory, manifests, candidates, and delivery requests reject unknown fields.
- Canonical examples remain mutually valid executable test vectors for schemas, cross-document rules, release hashes, and deterministic identities.
- Every semantic rejection has a regression test.
- Candidate and request identities are deterministic across replays.
- Feed checkpoints advance only after candidate and outbox work is durable.
- Untrusted network and source content is bounded, escaped, and excluded from sensitive logs.
- Production readiness includes measured feed quality, delivery capacity, recovery, and notification evidence.

## Implementation milestones

- [x] Define product scope, decisions, schemas, examples, semantic validation, and regression tests.
- [x] Build a historical announcement corpus and matching evaluation harness. Verify precision and recall targets per service and risk type, negative examples, edited announcements, overlapping feeds, missing publication dates, and deterministic replay.
- [x] Implement immutable release publishing and promotion. Verify hashes, exact object versions, compare-and-swap promotion, rollback, concurrent publishers, retention, and incompatible-version rejection.
- [x] Implement safe feed acquisition and source state. Verify host allowlisting, DNS/IP controls, TLS, no redirects, response and parser limits, validators, partial feed failures, provenance coalescing, raw snapshots, checkpoints, and per-feed freshness alarms.
- [x] Implement matching, profile mapping, candidate construction, and the durable outbox. Verify route isolation, sorted environment IDs, distinct service/risk evidence, revisions, provenance-only updates, identity vectors, candidate limits, and atomic checkpoint rules.
- [x] Implement dispatch, SQS FIFO transport, Slack delivery, and reconciliation. Verify message groups, dispatch dedupe, leases, destination pacing, retry classes, `Retry-After`, network-attempt accounting, every delivery state, crash boundaries, unknown outcomes, manual replay, and DLQ recovery. Publish the rendered sample named in the deliverables from that same renderer, so the documented message cannot drift from the one Slack receives.
- [x] Implement `infra/bootstrap` and `infra/central`. Verify remote-state permissions, native lockfile use, provider locks, encryption, IAM boundaries, schedules, indexes, TTL, alarms, and reproducible packages.
- [x] Complete production preflight and operational validation. Verify every destination, notification subscription, corpus quality, feed freshness, declared load envelope, dashboards, backup and restore where configured, shadow mode, rollback, and runbook exercises.

## Current state

A milestone is checked only when its whole verification list holds. All eight
implementation milestones now have their required source, deployment, and
evidence at the exact boundaries stated below. The repository's full check
target runs the test suite, and the committed corpus scores precision 1.000
and recall 1.000 across 29 true positives.

**Corpus and evaluation harness.** Complete. `corpus/announcements.json` holds 47 labeled announcements, 26 of them negative examples, with 29 expected positive matches. `src/evaluation.py` reports precision and recall per service and risk type. Edited announcements, overlapping feeds, missing publication dates, and deterministic replay are covered by tests. `corpus/thresholds.json` sets global floors only, and the harness already supports per-pair overrides. The observed counts do not justify those overrides: four of the ten pairs with any positive carry only one or two true positives. ADR-018's accepted 2026-09-06 revision requires a reviewed disposition for every enabled pair, reports recall as undefined where no labeled positive exists, and retains the global floors and explicit revisit triggers. It does not extend the sample merely to obtain a positive.

The repository owner selected the current 4-feed, 3-service, 4-risk-rule policy unchanged for production preflight on 2026-09-01. The [production policy evidence](evidence/production-policy.md) expands the review to all 12 configured service and risk-type pairs: 6 have no historical positive, 4 have one, and the remaining 2 have two and seven. Those limits remain explicit, the global floors still govern promotion, and L-43 accepted them within the passing M3 result.

**Immutable release publishing and promotion.** The write half is implemented. `src/releases.py` publishes both release objects with `If-None-Match: *`, verifies each by exact-version read-back, and compare-and-swaps the active pointer with `If-Match`, keeping ADR-019's 412, 409, and 404 outcomes distinct. A test rebuilds `examples/active-versions.json` from the committed configuration and inventory bytes, so the publisher is bound to the contract rather than to itself. `scripts/publish_release.py` supplies the clean-checkout operator boundary: it generates inventory version 3 from reviewed deployment input, checks captured Terraform bucket and prefix outputs, validates the deployment/configuration/inventory bundle before any S3 write, previews one canonical plan, applies only unchanged plan bytes and pointer state, and requires the compatibility probe before reporting completion. Injected-store and moto tests cover first promotion, matching adoption, stale inputs and pointer state, 412, both 409 results, 404, and failed probing. The command has now run against the dev bucket under the exact release-publisher role. It promoted release `6188e8d27ca14a9dbe898f8551f69a4813162cf8af9aef1d09461560cd8f4a9e`, read the active pointer and both release objects back by exact version, and passed the runtime compatibility probe. That step proved the first active dev release. The later D0 evidence below proves the watcher, dispatcher, and worker deployment plus one controlled real delivery. The read half loads the pointer, fetches the exact versions it pins, verifies their hashes, recomputes the release ID from those hashes, validates the fetched bodies against their owned schemas, and binds each document's internal version to the pointer claim. That is chapter 03's step-8 compatibility probe and the milestone's incompatible-version rejection. Rollback verifies a retained pointer version and writes its references forward through the same `If-Match` path, and every proposed promotion must record a parseable time after the pointer it replaces, which is what keeps a later write from reproducing a retained version's ETag. The forward-time revision is accepted as of 2026-08-09; an observed malformed timestamp remains replaceable so corrupt state can be repaired. Manifest retention is handled by the lifecycle rule on `infra/central`'s config bucket, which keeps `minimum_retained_releases` noncurrent versions of `active-versions.json` past `manifest_noncurrent_version_expiration_days`. `scripts/retire_config_releases.py` now supplies the missing retirement step. It binds a complete exact-version inventory and all retained manifest references into one canonical plan, protects additional release IDs declared for retained evidence, applies the 400-day and newest-10 floors, and deletes only exact ETag-bound versions after an unchanged recheck. A partial two-object deletion can resume only from that same plan when the fresh inventory proves no other drift. The release-publisher role can list versions for the exact manifest key and release prefix and delete versions only under the release prefix. The command has local simulated coverage; no live release retirement has run. The concurrent-promotion suite is now built and has run against the real bucket. ADR-019's testing revision, accepted 2026-08-07, puts the suite's bucket (`apcf-concurrency-dev`), its scoped identity (`apcf_concurrency_test`), and the prefix-expiring lifecycle rule in `infra/bootstrap`; `tests/test_s3_real_bucket.py` then runs the ADR-019 clauses a single request can express against S3 through the real `S3ObjectStore`, plus the headline assertion twelve publishers released against one observed ETag produce exactly one winner and eleven `412`s`. It passed four runs under the scoped identity on 2026-08-07, so milestone 2's "concurrent publishers" item now carries real-bucket evidence; the `409` branch remains unverified, which is a property of the outcome rather than a gap in effort. Access keys for the identity are created per run and deleted afterward, never committed.

**Safe feed acquisition and source state.** Implemented and deployed in the watcher Lambda with the exact shared package. Its 15-minute schedule is enabled. Host allowlisting, address validation, TLS pinning to a validated address, redirect refusal, distinct connection and response timeouts, response and parser limits, conditional requests, partial feed failures, and provenance coalescing have tests. DynamoDB adapters use expiring conditional leases and state versions; S3 stores the exact bounded response before parsing, with a collision-resistant key and safe metadata. Pending validators advance only after deterministic response pages, candidates, delivery records, and emission references survive read-back, followed by one all-or-none checkpoint transaction. Announcement and response-page writers now apply ADR-025 expiry metadata without shortening an existing boundary; a natural HTTP 200 cohort proved 91 exact announcement calculations and one page calculation in dev. On 2026-08-30, the one-time legacy migration added retention dates to 76 announcement rows and 88 response-page rows, left the 4 active feed checkpoints unchanged, and removed the temporary projected-scan role after verification. Retained-source replay now has an exact-snapshot, exact-release, preview-first command with canonical plan bytes, durable-state drift detection, explicit route scope, default suppression of existing candidates, and no feed-checkpoint permission. It has local in-memory and contract coverage; no live source replay has run. The watcher has a 300-second timeout, reserved concurrency one, a 360-second lease, and a 60-second claim reserve. Its artifact inputs must exactly equal the worker's. The aggregate freshness alarm reads dimensionless `MaxFeedStalenessSeconds`, while the dashboard discovers per-feed `FeedStalenessSeconds` series from the bounded `FeedName` dimension.

**Matching, profile mapping, candidate construction, and the durable outbox.** Implemented with an end-to-end service-mock test driving an exact S3 release pointer and raw feed bytes through to the committed candidate, raw snapshot, both DynamoDB tables, and an advanced checkpoint. Route isolation, sorted environment IDs, distinct service and risk evidence, revisions, provenance-only updates, out-of-order observation precedence, compare-and-swap merge, identity vectors, and candidate and request byte limits are covered. The outbox and announcement state both have in-memory and DynamoDB stores tested against moto.

**Dispatch, SQS transport, Slack delivery, and reconciliation.** The dispatcher is implemented behind ports and through a strict scheduled Lambda composition root. It merge-orders and caps due work across scheduled states, validates exact stored requests and their byte limit, conditionally claims a state version and monotonically increasing generation, reuses active claims after uncertain sends, sends through the SQS FIFO adapter, and conditionally records queue acceptance. Its one-minute handler validates the schedule and environment before heartbeat or store access, returns bounded counts, and has direct moto coverage from a due DynamoDB record through FIFO acceptance to `queued`. In-memory race tests cover lost claims, post-send write failure, and the full ABA cycle; moto also covers the DynamoDB conditions and SQS message body, group, and dedupe attributes. The Slack worker validates exact release and application artifact references, re-derives candidate semantics, renders bounded plain-text messages, claims leases, records every post-call outcome, schedules safe retries, and updates destination pacing from response-completion time. Its FIFO Lambda handler validates strict queue bodies, preserves the ordered suffix after failure, checks remaining invocation time, and emits bounded metrics. The recovery reconciler is implemented as a separate core and scheduled Lambda composition root. It uses bounded GSI observations, routes due scheduled work through the dispatcher, conditionally resolves exact expired leases to `delivery_unknown`, and signals old queued work without automatically resending it. Its state and race behavior has paired in-memory and moto coverage. One preview-first operator command now owns three bounded actions: found-post closure, audited `delivery_unknown` replay, and exact-request-compatible replay of a live `failed_terminal` record. Terminal replay uses ADR-021's exact response allowlist, preserves request/release/application identity and the historical network-attempt count, retains dedicated terminal evidence, and reserves one call through the existing dispatcher and worker. Paired in-memory and moto tests cover stale state, changed or expired TTL, changed outcome, existing reservation, history capacity, exhausted-budget success, and return to terminal after another retryable response. Another command validates the exact FIFO source/DLQ policy pair and controls native SQS movement through explicit start, status, and cancellation actions. The redrive controller reports approximate task counts, omits message bodies, and states that cancellation is not an exact message-count boundary. Terraform defines conditional dispatcher and reconciler runtimes; the dispatcher uses the worker's exact package pair and runs every minute with a 60-second timeout, reserved concurrency one, bounded retries, and source-scoped exhausted-event handling. The reconciler keeps independent exact package inputs and runs every five minutes with the same timeout and concurrency boundary. All four source-defined runtimes are deployed and enabled in persistent dev. The 12-message public-feed cohort reached durable `posted`, and the isolated recovery exercise proved one due record through Slack plus one expired `sending` record to `delivery_unknown` without another Slack attempt. The canonical candidate's public Slack sample is generated through `render_message`; site validation rejects renderer, fixture, or page drift.

Slice 2 uses its accepted capacity boundary. The configured 300-second Lambda timeout is the hard invocation bound, queue visibility is six times that value, and the FIFO handler keeps a 30-second reserve before starting another record. The sending lease uses the full function timeout. `slack_request_timeout_seconds` still bounds each blocking socket operation separately. A Lambda timeout after the sending claim becomes `delivery_unknown` through lease recovery. ADR-007 records the conditions that reopen this choice.

Application-package replay also has an accepted floor: retain content-addressed packages for at least 400 days and keep at least the newest 10. The deterministic builder installs a complete exact dependency lock for Python 3.12 on x86_64, the publisher conditionally creates and verifies a digest key in the versioned deployment bucket, and Terraform deploys the exact returned object version while injecting the same digest. The watcher, dispatcher, and worker use one exact digest and S3 VersionId; the reconciler has an independent input pair for the same shared package shape. Runtime source, production dependencies or lock, packaged schemas or assets, and builder-input changes require two same-toolchain builds with byte or digest comparison before handoff. Documentation, site, test-only, and Terraform-only changes do not trigger that comparison alone. On a version mismatch, the handler reports whether the required digest key is unavailable without changing delivery state. The runbook records pause, drain, publish, deploy, verify, and rollback steps. ADR-022 now supplies preview-bound retirement: it uses a complete bounded inventory, protects exact rollout and rollback pairs, requires an unchanged canonical plan for apply, deletes one exact conditioned version at a time, and reports partial or ambiguous outcomes without retry. Older unresolved evidence stays durable when its package is unavailable and requires an operator decision.

**Terraform roots.** Both roots exist, validate clean, and the prior baseline is applied to the dev deployment in account 667653114001. `infra/bootstrap/` provisions the private, versioned remote-state bucket `apcf-state-dev` with SSE-S3 encryption, a SecureTransport-denial bucket policy document, native S3 locking (`use_lockfile = true`), and a committed provider lockfile per ADR-006; it also provisions the ADR-019 concurrent-promotion bucket `apcf-concurrency-dev` (versioned, SSE-S3, with the two-rule prefix-expiring lifecycle) and the `apcf_concurrency_test` scoped identity for the operator-run suite. `tests/test_s3_real_bucket.py` uses that identity for operator-run concurrency checks. `infra/central/` decodes a reviewed `deployment.yaml` (committed as `infra/central/deployment.yaml`, validated against `deployment.schema.json`) and provisions the versioned config bucket `apcf-config-dev` with the release, manifest, raw-snapshot, and application-artifact prefixes and their lifecycle boundaries; the `apcf-source-state-dev` and `apcf-delivery-dev` DynamoDB tables with the `status-next-action-index` GSI and `expires_at` TTL; the encrypted `apcf-delivery-dev.fifo` queue and FIFO DLQ with `queue_max_receive_count` redrive and a 1,800-second visibility timeout; the separate encrypted runtime-failure queue; the Slack credential secret containers; the operational SNS topic and one confirmed email subscription joined from a reviewed descriptor to a private endpoint map; separate runtime and operator IAM roles; the disabled recovery-reconciler rule; and the source-defined log groups, dashboard, and alarms. The source-replay role is defined and validated locally. It awaits a reviewed central plan and apply before the command can run in dev.

The current central root was reconciled on 2026-08-21 with one reviewed saved plan under native locking. The plan contained 13 creates, 10 in-place updates, and 4 deletes. Terraform reported 13 added, 9 changed, and 4 destroyed because the deferred outbox-dispatcher policy resolved during apply without a provider modification. A complete follow-up plan reported no changes. The apply contained no Lambda function, event target, Lambda permission, event-source mapping, DynamoDB table, S3 bucket, Slack secret, package object, configuration release, or active-manifest action.

An audit of that work on 2026-08-07 found and fixed three IAM and lifecycle defects that `terraform validate` cannot see, since each is a policy or rule that applies cleanly and then matches nothing at runtime. A DynamoDB global secondary index is a distinct IAM resource, so the dispatcher and reconciler policies, which named only the table ARN, were implicitly denied `dynamodb:Query` on `status-next-action-index` — the query ADR-007 makes the dispatcher's entire job. The feed watcher was likewise denied `dynamodb:GetItem` on the delivery table, which `outbox.emit` calls before every write. Raw snapshots were never actually deleted, because an expiration rule on a versioned bucket writes a delete marker and leaves the body as a noncurrent version that the surrounding keep-10 rule could never expire. Each was confirmed against the deployed account with `aws iam simulate-principal-policy` and the applied bucket lifecycle, not by reading the HCL.

The watcher now produces the chapter 05 release-verification, raw-snapshot, aggregate freshness, and heartbeat metrics, with a reverse contract test from custom alarm names to runtime producers. `outbox-backlog-age` covers oldest unresolved outbox work, which the queue-age alarm cannot see because it observes only messages already in SQS. The operational email subscription is confirmed, matches the private endpoint source, and is now imported into Terraform ownership at its unchanged ARN. This proves subscription configuration and ownership. Ledger 22 did not inspect notification delivery; the later M1 exercise recorded the owner-confirmed receipt. Up to eight initial OK notifications could have resulted from the new alarms, but their receipt was not checked. `ReleaseVerificationFailures` is now diagnostic with no notification actions. The enabled runtime cohort has produced watcher, dispatcher, and reconciler heartbeats; all 28 dev alarms reached `OK` with actions enabled. The first watcher run after enablement had zero Lambda errors and throttles, and the runtime failure queue and delivery DLQ were empty in the bounded observation window.

The release-publisher policy permits `s3:PutObject`, `s3:GetObject`, and `s3:GetObjectVersion` in the exact application-artifact prefix while both object-delete actions remain implicit deny. IAM simulation and the exact assumed-role identity check passed. The deterministic package is now published at SHA-256 `1a3db4a6dba414c11cee875986704f0783c517eb07d49d2ce605eadd6839c1e1`, S3 VersionId `9zzQALVMVM6hSm1W8D5YIi49nbZ0wic5`; exact-version body and metadata checks passed under the scoped publisher role.

The watcher, dispatcher, worker, and recovery reconciler are deployed with the
exact package and their source-defined triggers are enabled. The 12-message
public-feed cohort reached Slack and durable `posted`; the isolated recovery and
fixed-load exercises passed; and the owner confirmed the alarm notification.
The L-43 production preflight has passed. Retirement commands remain
separately authorized operator actions and were not required for that result.

**M3 shadow and rollback proof.** The source now contains a direct-invocation
shadow evaluator that loads the exact active release and runs the production
fetch, parse, normalize, match, route, candidate, and durability orchestration
against fresh in-memory stores. Terraform gives it the watcher's exact package
and network policy but only release-read and log permissions. A separate role
can invoke only that function, and fixed refusal codes survive the Lambda
boundary. Accepted ADR-026 stops all durable runtimes and sets reserved
concurrency to zero for watcher, dispatcher, worker, and reconciler before
either rollback path. The release
publisher command now previews and applies an exact retained-pointer rollback,
then supports forward restoration from the former active VersionId through the
same path. Local tests cover identity inversion, no durable client construction,
stale rollback plans, rollback, forward restoration, and compatibility probing.
The owner accepted ADR-026 on 2026-09-05. The first live shadow invocation that
day reached the scoped invoker but failed before handler entry: the deployed
exact package predated `shadow_runtime.py`. Read-back found no source-state,
delivery, or raw-snapshot change, and the fixed attempt was not repeated. The
exercise stopped with a failed disposition. Package publication and Terraform
now define a configured-handler metadata guard. On 2026-09-06 an exact reviewed
plan disabled all four triggers and set watcher concurrency to zero, the full
300-second quiescence window elapsed, and a second exact reviewed plan deployed
one verified handler-complete package to all five Lambdas with both artifact
guards. Independent read-back matched every handler, code digest, object
VersionId, update status, and stopped-state control. Three separately authorized
identity inversions then reached that handler and returned the exact release,
application, and feed-set refusal codes. The 631 source-state items, 31 delivery
items, and 635 raw-snapshot versions remained unchanged, with no delete markers.
The following single valid sample fetched all four feeds, normalized 240 items,
matched 8 route-scoped candidates, and returned `passed` under invocation ID
`eff3c045-1959-477e-b5be-ade9c176a69d`; a second complete read-back was also
unchanged. Configuration rollback then promoted retained release
`0ffc94ed3c5a16d55561aa00f018c6cf6f1e81ec58539ce002f42c6e45eb7225`
through a new exact pointer version and passed its compatibility probe. Its one
shadow sample again normalized 240 items and produced the same 8 candidates and
candidate-identity digest. A preview-only retained-source replay resolved a
pre-exercise snapshot, pointer VersionId, exact release objects, and all four
existing candidate and delivery records without applying replay.

Forward restoration then returned the pointer to release
`8527e2b44432e565b968d869f941cc4a90ce33fbe7376401f5489306e6027ef8`
through VersionId `HJbvljTXyq.N1mExbfOiNn2Sv3vQnm5s`. A third shadow sample,
invocation ID `81a39324-9062-4b80-b3aa-d52c9454b430`, reproduced the same 240
items, 8 candidates, and candidate digest while another complete durable-state
comparison remained unchanged. The reviewed resume plan restored all four
triggers and watcher concurrency, created the seven missing alarms, and kept
the remediated application package on every Lambda. All 28 alarms reached
`OK`, and the final Terraform plan reported no changes.

The exact-version audit found nine retained application packages. Eight lacked
the shadow module and configured-handler metadata; only the currently deployed
package satisfied ADR-026's accepted five-function boundary. No distinct
eligible predecessor therefore existed at preflight, so application rollback
was not attempted. L-42 ends with the bounded `incomplete` disposition allowed
by its issue contract. The [public evidence
record](evidence/l42-shadow-and-rollback-2026-09-06.md) binds the public facts to
the restricted evidence manifest. Accepted ADR-029 now defines L-52's stable
in-archive input manifest, source-member and handler checks, S3 SHA-256
checksum, and Linux target import gate. It records volatile build details
outside package identity and defers signed hosted-build provenance until the
deployment's trust boundary grows. L-53 now has an accepted fixed exception
for that one checksum-less package, mandatory checksums for every other
selection, and a four-executor pause that keeps shadow callable. The exact S3
version has since passed byte, source-revision, archive, handler, and
network-disconnected Linux Python 3.12 x86_64 import qualification; all five
current Lambda configurations report the same package digest and their
expected handlers. Two byte-identical genuine L-52 successor builds passed the
new manifest and Linux gate, and scoped publication stored the exact
checksum-bearing `1ae996ca…` package at VersionId `yFAfr5Y8fdEnOatvqVHzXlFej5cXSSTg`.
That successor is deployed across all five functions. Its three identity
refusals, initial bounded shadow sample, and first natural scheduled cycle
passed. The owner accepted ADR-026's application-only L-54 revision on
2026-09-06. It reuses L-42's verified configuration claim when a recorded
comparison finds no unresolved material difference, requires read-only
retained-reference resolution, and makes configuration mutation never an
automatic fallback.

The [L-54 application rollback record](evidence/l54-application-rollback-2026-09-07.md)
now reports `passed`. The exact `c88b49c8…` predecessor and `1ae996ca…`
successor each passed a five-function transition, direct read-back, historical
reference check, and one bounded shadow invocation while all durable execution
remained stopped. Both samples processed four feeds and 240 items and returned
the same eight candidate identities. The baseline and final stopped-state
comparison matched both table digests, 673 raw-snapshot versions, queues,
active pointer, restored successor package, and every paused control. One
actual-state plan then restored the original concurrency, four triggers, and
seven derived alarms. A fixed natural observation recorded one watcher, 18
dispatcher, and three reconciler invocations with no errors or throttles; all
28 alarms were `OK`, queues and actionable states were empty, and the final
Terraform plan had no changes.

The [M3 readiness assessment](evidence/m3-production-readiness-assessment-2026-09-06.md)
uses that result for the actual one-environment, one-destination, four-feed,
three-service, four-rule deployment and its 300-delivery/hour envelope. Final
candidate `91f8db0e9b1f6cd6a1889face54589feaef295c0` passed every required
check in PR #196 and merged to `main` as
`5fc0dd2809fd6e256ee70508b52e8399fce803a3`. The owner accepted L-43's
terminal `passed` disposition on 2026-09-07. M3 is production-ready within the
assessment's recorded limits; L-42 remains a completed `incomplete` attempt
rather than being rewritten as a pass.

**M3 data recovery.** The owner selected PITR for both DynamoDB tables with a
35-day recovery period, a 5-minute recovery-point target, and a 4-hour operator
recovery-time target. Accepted ADR-027 restores both tables to one timestamp
under new names, repairs provider settings, validates a complete bounded item
inventory, moves every runtime table binding under disabled triggers, and
proves rollback before any restored-table runtime writes. The Terraform and
preview-first operator tooling are implemented in source. Accepted ADR-028
supplies separate, digest-bound CloudTrail evidence when an active destination
omits `RestoreSummary`.

The fresh dev proof completed on 2026-09-05 against commit `3bf35b7`. Recovery
plan SHA-256 `aa0f10e27f977c0f04ab7f3b8faa5ecdbc5222fad3e28cebbacd2dbe763a9a25`
and evidence SHA-256
`011258c9c89185ac94ead6afb6bc3f152d1f80d8bfc6c41ac8444d109cdc4899`
bind the result. Its shared restore point was 298 seconds behind the declared
start, meeting the nominal 5-minute target. Both full inventories, schemas,
tags, TTL settings, and 35-day PITR settings matched. A disabled-trigger
cutover moved every runtime, IAM, alarm, dashboard, and output reference to the
restored pair; rollback returned them to the primary pair with no inventory
change. All four triggers, watcher concurrency, and seven relevant alarms were
restored inside the 4-hour boundary, and the final Terraform plan
reported no changes. The four disposable restore tables from the successful
and superseded attempts were deleted and confirmed absent. L-41 is complete,
and L-43 accepted this bounded result as the recovery evidence for M3.

L-49 now binds recovery plan, evidence, and inventory digests to independent
known-answer tests. L-50 is also complete. The owner authorized saved central
plan `2bc02f0d47437a67b1be1546ab9dab5c5c88859f147bc5437d2507251d74986c`
from commit `4833343`; its only actions were two in-place updates enabling
DynamoDB deletion protection on the primary tables. Direct AWS reads returned
both tables `ACTIVE` with protection enabled, and a fresh central plan reported
no changes. The isolated preflight plan still creates both tables with
protection disabled, preserving reviewed teardown.

**Production preflight.** Complete. L-43's evidence matrix compares every
applicable criterion with its baseline and intervening changes. The exact
reviewed dev deployment passed its targeted live checks, evidence-reuse
decisions, final source validation, and required CI. This is not evidence for
repository-supported ceilings, another deployment, service on restored tables,
historical positives that do not exist, or exactly-once Slack delivery.

The watcher, dispatcher, worker, and reconciler have source-defined handlers,
metrics, packaging, and Terraform resources. Found-post reconciliation,
unknown-outcome replay, exact terminal replay, and native delivery-DLQ redrive
have preview-first operator commands. The canonical Slack sample comes from
the delivery renderer. Persistent feed and delivery operation, recovery, load,
alarm, rollback, and final production-readiness evidence are complete at their
recorded boundaries.

## Completion criteria

The goal is complete when a clean checkout can build and deploy the service, a production-like environment passes all automated and operator-confirmed preflight checks, public announcements produce reproducible route-scoped candidates, Slack delivery and recovery behave according to the accepted ADRs, and the documentation matches the implemented system.
