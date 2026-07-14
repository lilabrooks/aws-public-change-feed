# AWS Public Change Alerting

[![Status](https://img.shields.io/badge/status-architecture%20validated-00AA77)](#repository-status)
[![Repository quality](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/quality.yml/badge.svg?branch=main)](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/quality.yml)
[![Reference links](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/reference-links.yml/badge.svg?branch=main)](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/reference-links.yml)
[![Validators: Python 3.12+](https://img.shields.io/badge/validators-Python%203.12%2B-3776AB?logo=python&logoColor=white)](#local-validation)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

[![JSON Schema](https://img.shields.io/badge/contracts-JSON%20Schema-4B32C3?logo=json&logoColor=white)](schemas/)
[![Specs + ADRs](https://img.shields.io/badge/specs%20%2B%20ADRs-validated-00AA77)](docs/architecture/README.md)
[![OpenAI Codex](https://img.shields.io/badge/built%20with-OpenAI%20Codex-412991?logo=openai&logoColor=white)](AGENTS.md)

AWS Public Change Alerting turns public AWS announcements into route-scoped review work for teams that operate repeated AWS stacks. It fetches approved AWS feeds, matches announcements against declared services and risk rules, maps matches to potentially relevant environments, and delivers the resulting feed through Slack.

Slack is a delivery channel. The service's product is the filtered, explainable public-change feed and its durable candidate history.

## Why it exists

AWS publishes changes across What's New, blogs, and security bulletins. A general feed reader can collect those posts, but it does not know which services each environment uses, why a phrase is risky, or which team should review it. This project adds that missing context without requiring access to customer AWS accounts.

Each alert answers:

- What did AWS publish, and where is the source?
- Which configured service and risk rule matched?
- Which environment profiles make the item potentially relevant?
- Which Slack destination owns the review?
- Which exact configuration, inventory, and application version produced the result?

The service infers relevance from configuration. It does not claim that an account or resource is affected. Confirmed impact belongs in the operator's existing account-specific tools.

## Product boundary

Included:

- Safe RSS and Atom acquisition from approved public AWS hosts.
- Canonical announcement identity, revision tracking, and merged feed provenance.
- Deterministic service and risk matching.
- Static environment, profile, customer, and Slack-route mapping.
- Versioned `AlertCandidate` and `DeliveryRequest` contracts.
- A durable DynamoDB outbox, destination-grouped SQS FIFO transport, retry control, dedupe, and recovery.
- Slack incoming-webhook or bot-token delivery.
- Immutable configuration releases, alarms, replay evidence, and production preflight checks.

Excluded:

- Customer-account API access or cross-account roles.
- Account-specific event, security-finding, spend, or resource telemetry.
- Incident management, remediation, ticketing, acknowledgement workflows, or external platform adapters.
- A promise of exactly-once Slack posting.

[ADR-017](docs/adr/017-public-feed-only-product-scope.md) records this boundary.

## Processing flow

```mermaid
flowchart LR
    Feeds["Approved AWS public feeds"] --> Watcher["Feed watcher"]
    Watcher --> SourceState["Feed and announcement state"]
    Watcher --> Matcher["Service and risk matcher"]
    Release["Immutable config and inventory release"] --> Matcher
    Matcher --> Outbox["Candidate and delivery outbox"]
    Outbox --> Dispatcher["Outbox dispatcher"]
    Dispatcher --> Queue["SQS FIFO by destination"]
    Queue --> Worker["Slack delivery worker"]
    Worker --> Slack["Slack channel"]
    Worker --> Outbox
    Reconciler["Recovery reconciler"] --> Outbox
```

The watcher persists every candidate and delivery record before advancing a feed checkpoint. DynamoDB is the delivery system of record. SQS transports ready work and orders it per Slack destination. Unknown HTTP outcomes stop for operator review.

## Repository status

This repository currently contains a validated architecture package, contracts, examples, regression tests, and implementation requirements. The Terraform roots and Python runtime named by the design remain implementation milestones. Do not describe the repository as deployable until those artifacts and production acceptance tests exist.

## Start here

1. Read [the goal](docs/GOAL.md) for scope, outcomes, and the build sequence.
2. Read [the architecture index](docs/architecture/README.md) for document ownership and folder structure.
3. Read [the overview](docs/architecture/specification/01-overview.md), then continue through the numbered specification.
4. Review [the accepted decisions](docs/architecture/README.md#architecture-decision-records).
5. Inspect the validated files in `examples/` and their contracts in `schemas/`.

## Local validation

Python 3.12 or newer is required.

```bash
python3.12 -m venv .venv
. .venv/bin/activate
make install
make check
```

Run the network-backed reference check separately:

```bash
make references-online
```

The local validation covers JSON Schema, cross-document semantics, deterministic identities, rejected configuration mutations, local links, reference review dates, Python formatting, lint, type checking, YAML, and Git whitespace.

## Configuration model

- `examples/deployment.yaml` holds infrastructure-coupled settings, destinations, environment inventory inputs, retention, fetch limits, and the supported scale declaration.
- `examples/config.yaml` holds feeds, service definitions, profiles, environment policy, risk rules, and message limits.
- `examples/inventory.json` is the runtime projection of deployed environments and Slack routes.
- `examples/active-versions.json` binds exact configuration and inventory object versions into one immutable release.
- `examples/alert-candidate.json` and `examples/delivery-request.json` show the feed and delivery boundaries.

The examples contain placeholders. They are fixtures, not production credentials or account data.

## Security summary

The feed watcher has outbound HTTPS access only to approved feed hosts and no Slack secret access. The Slack worker is the only role that reads delivery credentials. Public source text is treated as untrusted, bounded during fetch and parsing, rendered as plain text, and retained only according to the configured lifecycle. Logs exclude feed bodies, secrets, and complete Slack payloads.

References verified: 2026-07-13.

## License

Copyright 2026 Lila Brooks.

Licensed under the [Apache License 2.0](LICENSE). Redistributed copies and
derivative works must preserve the attribution required by the license,
including [NOTICE](NOTICE).
