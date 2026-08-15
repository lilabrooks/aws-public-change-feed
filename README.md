# AWS Public Change Alerting

[![Status](https://img.shields.io/badge/status-contracts%20%2B%20feed%20pipeline%20validated-00AA77)](#validation-status)
[![Repository quality](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/quality.yml/badge.svg?branch=main)](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/quality.yml)
[![Reference links](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/reference-links.yml/badge.svg?branch=main)](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/reference-links.yml)
[![Architecture page](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/pages.yml/badge.svg?branch=main)](https://lilabrooks.github.io/aws-public-change-feed/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

[![JSON Schema](https://img.shields.io/badge/contracts-JSON%20Schema-4B32C3?logo=json&logoColor=white)](schemas/)
[![Specs + ADRs](https://img.shields.io/badge/specs%20%2B%20ADRs-included-00AA77)](docs/architecture/README.md)

AWS Public Change Alerting turns approved public AWS announcements into explainable, route-scoped Slack review candidates. It maps deterministic service and risk matches to potentially relevant environments without requiring customer-account access.

**[Read the public architecture page](https://lilabrooks.github.io/aws-public-change-feed/)** for the value proposition, design rationale, processing flow, system boundaries, and current evidence.

## Repository purpose

The repository holds the service and its authoritative architecture package. The contracts are settled and validated; the runtime and infrastructure are the current build. It contains:

- The product [goal and implementation milestones](docs/GOAL.md).
- A numbered [architecture specification](docs/architecture/README.md).
- Accepted [architecture decisions](docs/architecture/README.md#architecture-decision-records).
- Strict machine-readable [schemas](schemas/) and one canonical [example bundle](examples/).
- The [feed pipeline runtime](src/aws_public_change_feed/) and the [labeled corpus](corpus/) its matching quality is measured against.
- Semantic validators and regression tests for cross-document rules and deterministic identities.

Public announcements provide review evidence. They do not prove that an AWS account, environment, or resource is affected. Operators confirm applicability with their existing account-specific tools.

## Validation status

The `contracts + feed pipeline validated` badge means the committed artifacts and the implemented runtime pass the repository's automated checks:

- Each canonical example passes its paired JSON Schema.
- The complete example bundle passes projections, references, route, release-hash, identity, retention, and size checks.
- Regression tests confirm that rejected configuration and event-contract mutations fail validation.
- Python quality, YAML, local links, reference dates, the public page, and Git whitespace pass the same gate.
- Deterministic matching scores at or above the approved thresholds against the labeled corpus.
- Feed acquisition refuses unapproved hosts, non-public resolved addresses, redirects, unsupported content types, oversized responses, and any XML carrying a DOCTYPE.
- Route-scoped candidates rebuild the committed example bundle field for field, binding the runtime to the contract rather than to its own output.
- The durable outbox keeps a stored candidate immutable under a newer release, repairs a missing delivery record from the stored candidate, and reports a stored item whose identity disagrees with its key as a correctness failure.
- One test drives the whole chain, from a raw feed response through matching and candidate construction to a feed checkpoint that advances only once the outbox records exist.

The implemented runtime now covers scheduled feed acquisition, exact raw snapshots, conditional DynamoDB source state, normalization, announcement identity, deterministic matching, profile and route mapping, candidate construction, the durable outbox creation boundary, the DynamoDB delivery store, due-work dispatch, the SQS FIFO sender, the Slack worker, and bounded delivery recovery. A service-mock composition test follows one exact S3 release pointer and raw RSS response through the watcher into S3 snapshot evidence and both DynamoDB tables. Its corpus mixes real announcements taken from the four configured feeds with authored items covering shapes the recent feed window did not contain. No end-of-support announcement appeared in that window, so recall for that risk type rests on authored items alone.

The applied Terraform foundation includes the config bucket, DynamoDB tables, FIFO queue, DLQ, IAM roles, log groups, alarms, and dashboard. Source now defines four conditional Lambda composition roots. The watcher loads an exact immutable release, enforces the deployment feed policy before claims, uses expiring conditional leases, snapshots exact responses before parsing, merges announcement revisions with compare-and-swap, and advances fetched validators only after paged durable evidence is read back and one batch transaction succeeds. The regular dispatcher runs every minute, composes the existing DynamoDB and FIFO adapters, and preserves exact requests and durable dispatch claims. The Slack worker binds FIFO messages to durable delivery state and stops before its invocation reserve. The scheduled reconciler performs bounded indexed observation and exact recovery transitions. One preview-first operator command conditionally records a found Slack post, reserves an audited `delivery_unknown` retry, or reserves one exact-request-compatible replay of a live terminal record. Terminal replay follows [ADR-021](docs/adr/021-audited-terminal-record-replay.md)'s exact allowlist, preserves the historical attempt count and immutable request/release/application identity, and refuses expired, changed, malformed, unknown, and immutable failures. Another command validates the exact FIFO source/DLQ pair and controls native SQS redrive through explicit start, status, and cancellation actions. Terraform gives each schedule bounded retries and source-scoped failure-queue access, and requires the watcher, dispatcher, and worker to share one exact package digest and S3 version. It also source-defines operational SNS subscription ownership by joining reviewed aliases and protocols to private sensitive endpoints, and scopes watcher AWS/Lambda error paging to enabled runtime while keeping release-verification alarms diagnostic. These source-defined resources remain unapplied. The existing operational subscription must be imported before an apply under separate authority. The deterministic Linux builder and append-only S3 publisher bind deployments to reviewed bytes. ADR-022 adds preview-bound exact-version retirement with separate IAM authority, complete bounded inventory, protected references, and explicit partial or ambiguous outcomes. The public page now carries the canonical candidate through the delivery renderer, with a generated-region check that catches fixture, renderer, or page drift. Live AWS and Slack evidence, load testing, and production preflight remain.

## Start here

1. Read the [public architecture page](https://lilabrooks.github.io/aws-public-change-feed/).
2. Read the [goal](docs/GOAL.md) for scope, milestones, and completion criteria.
3. Follow the [numbered specification](docs/architecture/README.md) in order.
4. Inspect the executable contracts in [`schemas/`](schemas/) and [`examples/`](examples/).
5. Run `make evaluate-corpus` to score matching against [`corpus/`](corpus/).

## Local validation

Python 3.12 or newer is required.

```bash
python3.12 -m venv .venv
. .venv/bin/activate
make install
make check
```

Run the network-backed checks separately:

```bash
make references-online
```

```bash
make screen-feeds
```

`screen-feeds` fetches the live feeds through the runtime acquisition path and reports every match, flagging any the corpus does not represent.

References verified: 2026-07-13.

## License

Copyright 2026 Lila Brooks.

Licensed under the [Apache License 2.0](LICENSE). Redistributed copies and derivative works must preserve the attribution required by the license, including [NOTICE](NOTICE).
