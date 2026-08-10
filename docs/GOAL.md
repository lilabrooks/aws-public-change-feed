# Goal: AWS Public Change Alerting

## Outcome

Deliver an AWS-hosted service that converts approved public AWS announcements into a filtered, explainable feed of review candidates and delivers that feed to the correct Slack destinations.

The service helps a team operating repeated AWS stacks answer a narrow question: which public AWS changes deserve review for the services declared in each environment profile? It infers possible relevance from configuration and preserves the source evidence. It does not assert confirmed account or resource impact.

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
- [ ] Build a historical announcement corpus and matching evaluation harness. Verify precision and recall targets per service and risk type, negative examples, edited announcements, overlapping feeds, missing publication dates, and deterministic replay.
- [ ] Implement immutable release publishing and promotion. Verify hashes, exact object versions, compare-and-swap promotion, rollback, concurrent publishers, retention, and incompatible-version rejection.
- [ ] Implement safe feed acquisition and source state. Verify host allowlisting, DNS/IP controls, TLS, no redirects, response and parser limits, validators, partial feed failures, provenance coalescing, raw snapshots, checkpoints, and per-feed freshness alarms.
- [ ] Implement matching, profile mapping, candidate construction, and the durable outbox. Verify route isolation, sorted environment IDs, distinct service/risk evidence, revisions, provenance-only updates, identity vectors, candidate limits, and atomic checkpoint rules.
- [ ] Implement dispatch, SQS FIFO transport, Slack delivery, and reconciliation. Verify message groups, dispatch dedupe, leases, destination pacing, retry classes, `Retry-After`, network-attempt accounting, every delivery state, crash boundaries, unknown outcomes, manual replay, and DLQ recovery. Publish the rendered sample named in the deliverables from that same renderer, so the documented message cannot drift from the one Slack receives.
- [ ] Implement `infra/bootstrap` and `infra/central`. Verify remote-state permissions, native lockfile use, provider locks, encryption, IAM boundaries, schedules, indexes, TTL, alarms, and reproducible packages.
- [ ] Complete production preflight and operational validation. Verify every destination, notification subscription, corpus quality, feed freshness, declared load envelope, dashboards, backup and restore where configured, shadow mode, rollback, and runbook exercises.

## Current state

A milestone is checked only when its whole verification list holds. Several unchecked milestones carry substantial working code, so this section records where each one actually stands. The repository's full check target runs the test suite, and the committed corpus scores precision 1.000 and recall 1.000 across 27 true positives.

**Corpus and evaluation harness.** Built. `corpus/announcements.json` holds 44 labeled announcements, 25 of them negative examples, and `src/evaluation.py` reports precision and recall per service and risk type. Edited announcements, overlapping feeds, missing publication dates, and deterministic replay are covered by tests. One gap keeps it unchecked, and it is not the one it looks like. `corpus/thresholds.json` sets global floors only, and the harness already supports per-pair overrides, so adding them is a config edit. The counts do not justify it: four of the ten service and risk-type pairs carry one or two true positives, where a recall floor is a demand never to miss a single item and one relabelling gates promotion on noise. The schema says the same thing, keeping overrides absent until measurements justify one. Closing this needs more labelled items in the thin pairs, which needs elapsed time or an archive the four configured feeds do not reach.

**Immutable release publishing and promotion.** The write half is implemented. `src/releases.py` publishes both release objects with `If-None-Match: *`, verifies each by exact-version read-back, and compare-and-swaps the active pointer with `If-Match`, keeping ADR-019's 412, 409, and 404 outcomes distinct. A test rebuilds `examples/active-versions.json` from the committed configuration and inventory bytes, so the publisher is bound to the contract rather than to itself. The read half loads the pointer, fetches the exact versions it pins, verifies their hashes, recomputes the release ID from those hashes, validates the fetched bodies against their owned schemas, and binds each document's internal version to the pointer claim. That is chapter 03's step-8 compatibility probe and the milestone's incompatible-version rejection. Rollback verifies a retained pointer version and writes its references forward through the same `If-Match` path, and every proposed promotion must record a parseable time after the pointer it replaces, which is what keeps a later write from reproducing a retained version's ETag. The forward-time revision is accepted as of 2026-08-09; an observed malformed timestamp remains replaceable so corrupt state can be repaired. Manifest retention is handled by the lifecycle rule on `infra/central`'s config bucket, which keeps `minimum_retained_releases` noncurrent versions of `active-versions.json` past `manifest_noncurrent_version_expiration_days`. Release retirement is not, and an earlier rule that claimed to do it has been removed: release objects are write-once at a per-release key, so they have no noncurrent versions for such a rule to reach, and it expired nothing. Chapter 05 records why the age-based alternative is unsafe and why retirement belongs to the publisher. `retired_release_retention_days` and `minimum_retained_releases` remain validated deployment inputs with no enforcement behind them until that publisher-side step exists. The concurrent-promotion suite is now built and has run against the real bucket. ADR-019's testing revision, accepted 2026-08-07, puts the suite's bucket (`apcf-concurrency-dev`), its scoped identity (`apcf_concurrency_test`), and the prefix-expiring lifecycle rule in `infra/bootstrap`; `tests/test_s3_real_bucket.py` then runs the ADR-019 clauses a single request can express against S3 through the real `S3ObjectStore`, plus the headline assertion twelve publishers released against one observed ETag produce exactly one winner and eleven `412`s. It passed four runs under the scoped identity on 2026-08-07, so milestone 2's "concurrent publishers" item now carries real-bucket evidence; the `409` branch remains unverified, which is a property of the outcome rather than a gap in effort. Access keys for the identity are created per run and deleted afterward, never committed.

**Safe feed acquisition and source state.** Implemented behind ports. Host allowlisting, address validation, TLS pinning to a validated address, redirect refusal, response and parser limits, conditional requests, partial feed failures, provenance coalescing, and checkpoints all have tests. The source-state table, the raw-snapshot prefix and its lifecycle rules, and a feed-freshness alarm now exist in `infra/central`. That alarm is one threshold over the dimensionless `MaxFeedStalenessSeconds` aggregate, not one alarm per feed: the feed list lives in `config.yaml`, a release artifact the Terraform root never reads, so per-feed alarms cannot be enumerated at plan time. The dashboard discovers `FeedStalenessSeconds` series by their bounded `FeedName` dimension for attribution. The watcher Lambda and its schedule arrive when the acquisition runtime is packaged as a function.

**Matching, profile mapping, candidate construction, and the durable outbox.** Implemented behind ports, with an end-to-end test driving raw feed bytes through to an advanced checkpoint and reproducing the committed candidate. Route isolation, sorted environment IDs, distinct service and risk evidence, revisions, provenance-only updates, identity vectors, and candidate and request byte limits are covered. Announcement state remains in-memory and feed state adds a file-backed store for local replay across runs. The outbox has both in-memory and DynamoDB stores; the latter targets the provisioned delivery table and is tested against moto.

**Dispatch, SQS transport, Slack delivery, and reconciliation.** The dispatcher is implemented behind ports. It merge-orders and caps due work across scheduled states, validates exact stored requests and their byte limit, conditionally claims a state version and monotonically increasing generation, reuses active claims after uncertain sends, sends through the SQS FIFO adapter, and conditionally records queue acceptance. In-memory race tests cover lost claims, post-send write failure, and the full ABA cycle; moto covers the DynamoDB conditions and SQS message body, group, and dedupe attributes. The Slack worker core is also implemented behind ports. It validates exact release and application artifact references, re-derives candidate semantics, renders bounded plain-text messages, claims leases, records every post-call outcome, schedules safe retries, and updates destination pacing from response-completion time. DynamoDB worker transitions have moto-backed coverage. Destination serialization still depends on the future FIFO batch handler using one message group per destination. The worker's two production adapters now exist behind those ports. The Slack HTTP transport covers both delivery modes, shares feed acquisition's address validation and pinned TLS connection rather than reimplementing them, reports only observed facts, and claims that no request byte was sent solely where connecting explicitly proves it; its `bytes_sent` truth table is covered row by row. Credential readers cover both accepted `secret_store` values, return the stored value whole with a configured kind, and keep the value out of every message, `repr`, and log. Neither contacts Slack or AWS in tests. Credential reads resolve by whether another identical read could succeed: permanent conditions resolve terminally, and transient store failures reschedule with a bounded delay, no Slack call, an unchanged network-attempt budget, and no pacing advance. The Lambda handlers, metrics adapter, FIFO batch handling, recovery reconciler, artifact packaging and retention, rollout procedure, and rendered sample remain unbuilt.

One named prerequisite gates Slice 2's completion. `slack_request_timeout_seconds` currently reaches blocking socket operations separately and does not prove a complete wall-clock request deadline, because DNS resolution and multiple blocking phases are involved. Before the FIFO Lambda handler, event source mapping, and timeout-derived capacity are called complete, the repository must either enforce an end-to-end monotonic deadline, including bounded DNS, or replace the capacity calculation with a provable upper bound. The adapter bounds its individual socket operations today; it is not yet evidence of an end-to-end request-duration bound, and no status text here claims otherwise.

**Terraform roots.** Both roots exist, validate clean, and are applied to the dev deployment in account 667653114001. `infra/bootstrap/` provisions the private, versioned remote-state bucket `apcf-state-dev` with SSE-S3 encryption, a SecureTransport-denial bucket policy, a committed backend-principal policy document, native S3 locking (`use_lockfile = true`), and a committed provider lockfile per ADR-006; it also provisions the ADR-019 concurrent-promotion bucket `apcf-concurrency-dev` (versioned, SSE-S3, with the two-rule prefix-expiring lifecycle) and the `apcf_concurrency_test` scoped identity for the operator-run suite. `infra/central/` decodes a reviewed `deployment.yaml` (committed as `infra/central/deployment.yaml`, validated against `deployment.schema.json`) and provisions the versioned config bucket `apcf-config-dev` with the release, manifest, and raw-snapshot prefixes and their lifecycle rules; the `apcf-source-state-dev` and `apcf-delivery-dev` DynamoDB tables with the `status-next-action-index` GSI and `expires_at` TTL; the encrypted `apcf-delivery-dev.fifo` queue and FIFO DLQ with `queue_max_receive_count` redrive and ADR-007's timeout-derived visibility; the Slack credential secret containers; the operational SNS topic; the five least-privilege IAM roles from chapter 05 (release publisher, feed watcher, outbox dispatcher, slack worker, recovery reconciler), including ADR-019's `s3:GetObject`/`s3:GetObjectVersion` split; and the log groups, operations dashboard, and alarms.

An audit of that work on 2026-08-07 found and fixed three IAM and lifecycle defects that `terraform validate` cannot see, since each is a policy or rule that applies cleanly and then matches nothing at runtime. A DynamoDB global secondary index is a distinct IAM resource, so the dispatcher and reconciler policies, which named only the table ARN, were implicitly denied `dynamodb:Query` on `status-next-action-index` — the query ADR-007 makes the dispatcher's entire job. The feed watcher was likewise denied `dynamodb:GetItem` on the delivery table, which `outbox.emit` calls before every write. Raw snapshots were never actually deleted, because an expiration rule on a versioned bucket writes a delete marker and leaves the body as a noncurrent version that the surrounding keep-10 rule could never expire. Each was confirmed against the deployed account with `aws iam simulate-principal-policy` and the applied bucket lifecycle, not by reading the HCL.

Two chapter 05 alarms have no counterpart yet. `outbox-backlog-age` now covers oldest unresolved outbox work, which the queue-age alarm cannot see because it only observes messages already in SQS. Confirmation that operational notification delivery has been tested stays with production preflight. The operational topic currently has zero subscriptions, so every alarm publishes to nobody; subscribing the reviewed endpoints is preflight work.

What remains is the deployed runtime: the four Lambda handlers, their schedules and event source mappings, DynamoDB source-state operations, S3 snapshot storage, application artifact retention and rollout, and recovery. The delivery-table adapter, SQS producer adapter, dispatcher, worker state machine, Slack HTTP transport, and credential readers exist, though no function invokes them in AWS. Because no runtime is deployed yet, none of the IAM defects above had produced a failure; they would have surfaced at the dispatcher's first invocation.

**Production preflight.** Not started, and blocked on the milestones above.

No box is one edit away. The corpus one looks closest and is not: its remaining gap is labelled depth in the thin pairs rather than a threshold setting. The largest missing piece is deployment and recovery around the built worker core and its adapters: FIFO batch handling, metrics, recovery, content-addressed Lambda packaging and retention, rollout, and the schedules and event source mappings that wire the tested ports to deployed resources. Until the event source mapping exists, no deployed component uses either adapter, so per-destination serialization remains unproven outside the worker's own per-record claims.

## Completion criteria

The goal is complete when a clean checkout can build and deploy the service, a production-like environment passes all automated and operator-confirmed preflight checks, public announcements produce reproducible route-scoped candidates, Slack delivery and recovery behave according to the accepted ADRs, and the documentation matches the implemented system.
