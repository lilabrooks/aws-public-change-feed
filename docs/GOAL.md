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
- [ ] Implement dispatch, SQS FIFO transport, Slack delivery, and reconciliation. Verify message groups, dispatch dedupe, leases, destination pacing, retry classes, `Retry-After`, network-attempt accounting, every delivery state, crash boundaries, unknown outcomes, manual replay, and DLQ recovery.
- [ ] Implement `infra/bootstrap` and `infra/central`. Verify remote-state permissions, native lockfile use, provider locks, encryption, IAM boundaries, schedules, indexes, TTL, alarms, and reproducible packages.
- [ ] Complete production preflight and operational validation. Verify every destination, notification subscription, corpus quality, feed freshness, declared load envelope, dashboards, backup and restore where configured, shadow mode, rollback, and runbook exercises.

## Completion criteria

The goal is complete when a clean checkout can build and deploy the service, a production-like environment passes all automated and operator-confirmed preflight checks, public announcements produce reproducible route-scoped candidates, Slack delivery and recovery behave according to the accepted ADRs, and the documentation matches the implemented system.
