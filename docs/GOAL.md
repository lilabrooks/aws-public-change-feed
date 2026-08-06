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

A milestone is checked only when its whole verification list holds. Several unchecked milestones carry substantial working code, so this section records where each one actually stands. The repository runs 350 tests and scores the committed corpus at precision 1.000 and recall 1.000 across 27 true positives.

**Corpus and evaluation harness.** Built. `corpus/announcements.json` holds 44 labeled announcements, 25 of them negative examples, and `src/evaluation.py` reports precision and recall per service and risk type. Edited announcements, overlapping feeds, missing publication dates, and deterministic replay are covered by tests. One gap keeps it unchecked: `corpus/thresholds.json` sets global floors only, so a single service or risk type degrading does not gate promotion even though the harness prints its figures. Per-pair overrides are supported and unset.

**Immutable release publishing and promotion.** The write half is implemented. `src/releases.py` publishes both release objects with `If-None-Match: *`, verifies each by exact-version read-back, and compare-and-swaps the active pointer with `If-Match`, keeping ADR-019's 412, 409, and 404 outcomes distinct. A test rebuilds `examples/active-versions.json` from the committed configuration and inventory bytes, so the publisher is bound to the contract rather than to itself. The read half loads the pointer, fetches the exact versions it pins, verifies their hashes, and refuses a release whose schema versions this build does not implement, which is chapter 03's step-8 compatibility probe and the milestone's incompatible-version rejection. Rollback and retention are not implemented. Concurrent-publisher behavior cannot be verified against the mock at all, for the reason ADR-019's milestone-2 testing section measures and records.

**Safe feed acquisition and source state.** Implemented behind ports. Host allowlisting, address validation, TLS pinning to a validated address, redirect refusal, response and parser limits, conditional requests, partial feed failures, provenance coalescing, and checkpoints all have tests. Raw snapshots have an in-memory store only, and per-feed freshness alarms are CloudWatch resources that arrive with the Terraform roots.

**Matching, profile mapping, candidate construction, and the durable outbox.** Implemented behind ports, with an end-to-end test driving raw feed bytes through to an advanced checkpoint and reproducing the committed candidate. Route isolation, sorted environment IDs, distinct service and risk evidence, revisions, provenance-only updates, and identity vectors are covered. The announcement-state and outbox stores are in-memory and feed state adds a file-backed store for local replay across runs; the DynamoDB adapters behind all three arrive with the Terraform roots that create the table.

**Dispatch, SQS transport, Slack delivery, and reconciliation.** Not started. No dispatch or Slack module exists. The rendered sample this milestone must publish is recorded as required evidence and has not been produced.

**Terraform roots.** Not started. `infra/bootstrap/` and `infra/central/` do not exist yet, which is why every adapter above is still in-memory and why no deployed behavior has been demonstrated.

**Production preflight.** Not started, and blocked on the milestones above.

The shortest path to checking a box is the corpus one, which needs per-pair thresholds. The largest missing piece is the Terraform roots, because they are what turn the ports into deployed behavior and unblock the evidence the last milestone requires.

## Completion criteria

The goal is complete when a clean checkout can build and deploy the service, a production-like environment passes all automated and operator-confirmed preflight checks, public announcements produce reproducible route-scoped candidates, Slack delivery and recovery behave according to the accepted ADRs, and the documentation matches the implemented system.
