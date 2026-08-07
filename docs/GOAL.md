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

A milestone is checked only when its whole verification list holds. Several unchecked milestones carry substantial working code, so this section records where each one actually stands. The repository runs 359 tests and scores the committed corpus at precision 1.000 and recall 1.000 across 27 true positives.

**Corpus and evaluation harness.** Built. `corpus/announcements.json` holds 44 labeled announcements, 25 of them negative examples, and `src/evaluation.py` reports precision and recall per service and risk type. Edited announcements, overlapping feeds, missing publication dates, and deterministic replay are covered by tests. One gap keeps it unchecked, and it is not the one it looks like. `corpus/thresholds.json` sets global floors only, and the harness already supports per-pair overrides, so adding them is a config edit. The counts do not justify it: four of the ten service and risk-type pairs carry one or two true positives, where a recall floor is a demand never to miss a single item and one relabelling gates promotion on noise. The schema says the same thing, keeping overrides absent until measurements justify one. Closing this needs more labelled items in the thin pairs, which needs elapsed time or an archive the four configured feeds do not reach.

**Immutable release publishing and promotion.** The write half is implemented. `src/releases.py` publishes both release objects with `If-None-Match: *`, verifies each by exact-version read-back, and compare-and-swaps the active pointer with `If-Match`, keeping ADR-019's 412, 409, and 404 outcomes distinct. A test rebuilds `examples/active-versions.json` from the committed configuration and inventory bytes, so the publisher is bound to the contract rather than to itself. The read half loads the pointer, fetches the exact versions it pins, verifies their hashes, recomputes the release ID from those hashes, and refuses a release whose schema versions this build does not implement, which is chapter 03's step-8 compatibility probe and the milestone's incompatible-version rejection. Rollback verifies a retained pointer version and writes its references forward through the same `If-Match` path, and every promotion must record a time after the pointer it replaces, which is what keeps a later write from reproducing a retained version's ETag. Retention is now handled by the lifecycle rule in `infra/central`'s config bucket, which keeps `minimum_retained_releases` noncurrent release versions past `retired_release_retention_days` and preserves the manifest's noncurrent versions per `s3_lifecycle`. The concurrent-promotion suite remains pending: ADR-019's revision runs it against a dedicated bucket that `infra/bootstrap` still has to gain, along with the scoped identity and the lifecycle rule that expires its key prefixes.

**Safe feed acquisition and source state.** Implemented behind ports. Host allowlisting, address validation, TLS pinning to a validated address, redirect refusal, response and parser limits, conditional requests, partial feed failures, provenance coalescing, and checkpoints all have tests. The source-state table, raw-snapshot prefix, and per-feed freshness alarms now exist in `infra/central`; the watcher Lambda and its schedule arrive when the acquisition runtime is packaged as a function.

**Matching, profile mapping, candidate construction, and the durable outbox.** Implemented behind ports, with an end-to-end test driving raw feed bytes through to an advanced checkpoint and reproducing the committed candidate. Route isolation, sorted environment IDs, distinct service and risk evidence, revisions, provenance-only updates, and identity vectors are covered. The announcement-state and outbox stores are in-memory and feed state adds a file-backed store for local replay across runs; the DynamoDB tables those adapters target now exist, and the adapters themselves land with the watcher and dispatcher Lambda packaging.

**Dispatch, SQS transport, Slack delivery, and reconciliation.** Not started. No dispatch or Slack module exists. The rendered sample this milestone must publish is recorded as required evidence and has not been produced.

**Terraform roots.** Both roots exist, validate clean, and are applied to the dev deployment in account 667653114001. `infra/bootstrap/` provisions the private, versioned remote-state bucket `apcf-state-dev` with SSE-S3 encryption, a SecureTransport-denial bucket policy, a committed backend-principal policy document, native S3 locking (`use_lockfile = true`), and a committed provider lockfile per ADR-006. `infra/central/` decodes a reviewed `deployment.yaml` (committed as `infra/central/deployment.yaml`, validated against `deployment.schema.json`) and provisions the versioned config bucket `apcf-config-dev` with the release, manifest, and raw-snapshot prefixes and their lifecycle rules; the `apcf-source-state-dev` and `apcf-delivery-dev` DynamoDB tables with the `status-next-action-index` GSI and `expires_at` TTL; the encrypted `apcf-delivery-dev.fifo` queue and FIFO DLQ with `queue_max_receive_count` redrive and ADR-007's timeout-derived visibility; the Slack credential secret containers; the operational SNS topic; the five least-privilege IAM roles from chapter 05 (release publisher, feed watcher, outbox dispatcher, slack worker, recovery reconciler), including ADR-019's `s3:GetObject`/`s3:GetObjectVersion` split; and the log groups, operations dashboard, and alarm set from chapter 05. What remains is the runtime half: the four Lambda functions, their schedules and event source mappings, and the DynamoDB/SQS/Secrets adapters that turn the in-memory ports into deployed behavior.

**Production preflight.** Not started, and blocked on the milestones above.

No box is one edit away. The corpus one looks closest and is not: its remaining gap is labelled depth in the thin pairs rather than a threshold setting. The largest missing piece is the runtime delivery: dispatch and Slack packages do not exist, and the Terraform data plane they consume is now provisioned, so the remaining work is the Lambda packaging, the DynamoDB/SQS/Secrets adapters, and the schedules and event source mappings that wire the ports to the deployed resources.

## Completion criteria

The goal is complete when a clean checkout can build and deploy the service, a production-like environment passes all automated and operator-confirmed preflight checks, public announcements produce reproducible route-scoped candidates, Slack delivery and recovery behave according to the accepted ADRs, and the documentation matches the implemented system.
