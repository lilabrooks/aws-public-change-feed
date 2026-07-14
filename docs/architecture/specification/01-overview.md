# 1. Overview and product boundary

## Purpose

AWS Public Change Alerting produces an explainable feed from public AWS announcements. It fetches approved RSS or Atom sources, identifies configured services and risks, maps those matches to environment profiles and Slack routes, persists route-scoped candidates, and delivers them through Slack.

The key distinction is evidence. A public announcement plus a configured profile establishes potential relevance. It does not establish that a customer account or resource is affected. Candidate fields, Slack messages, metrics, and documentation must preserve that wording.

## Value proposition

The service closes the gap between a broad public news stream and account-specific monitoring. General feed readers lack environment context. Account-specific tools commonly lack a deterministic review of public announcements against a declared shared stack. This service supplies:

- One canonical history for overlapping AWS feeds and edited announcements.
- Deterministic, reviewable service and risk matching.
- Static environment context without customer-account credentials.
- Route isolation and grouped review work for repeated stacks.
- Exact release and application provenance for every result.
- Durable Slack delivery state, retry, dedupe, and recovery evidence.

## Product boundary

The product owns public-feed acquisition through terminal Slack delivery state. Slack is replaceable only through a future scope decision and contract revision; the current product has no generic downstream adapter.

The service does not access customer accounts, discover resources, collect spend, ingest account events or findings, execute changes, open tickets, or track incident acknowledgement. Operators use the source link and their existing account-specific tools to verify applicability.

## Trust boundaries

1. **Public network:** feed hosts and content are untrusted. The fetcher applies an allowlist, DNS and IP checks, TLS validation, no redirects, byte and item limits, parser limits, and timeouts.
2. **Release boundary:** configuration and inventory are reviewed inputs. Runtime loads exact immutable S3 object versions and verifies their hashes.
3. **State boundary:** DynamoDB holds source checkpoints, announcement history, candidates, destinations, and delivery outcomes. Conditional writes govern concurrency.
4. **Transport boundary:** SQS FIFO carries ready delivery requests. It does not replace the durable outbox.
5. **Credential boundary:** only the Slack worker reads Slack credentials. Source content and contracts contain no secret values.
6. **External side effect:** a Slack HTTP request cannot share a transaction with DynamoDB. Ambiguous outcomes become `delivery_unknown`.

## Logical components

### Release publisher

Validates deployment, configuration, inventory, and cross-document invariants. It writes immutable release objects and promotes the active manifest with compare-and-swap semantics.

### Feed watcher

Runs on a schedule, loads one release, fetches all enabled feeds, normalizes and coalesces announcements, matches them, creates route-scoped candidates, persists outbox records, and then advances feed validators.

### Outbox dispatcher

Queries work due by `status` and `next_action_at`, sends exact delivery requests to SQS FIFO, and records successful queue acceptance.

### Slack delivery worker

Validates requests, loads their exact release, applies destination pacing, conditionally claims delivery work, renders safe Slack messages, performs the network call, and records the explicit outcome.

### Recovery reconciler

Repairs pending dispatch, re-enqueues eligible work, converts stale `sending` leases to `delivery_unknown`, extends unresolved retention, and emits recovery metrics.

## Deployment topology

Deploy shared resources in one AWS account and Region chosen by the operator. Customer account IDs and Regions are inventory metadata only. No organization membership or cross-account trust is required.

Two Terraform roots are planned:

- `infra/bootstrap` for the remote-state bucket and optional key.
- `infra/central` for the service resources.

The initial implementation uses Lambda, EventBridge Scheduler, DynamoDB, S3, SQS FIFO, Secrets Manager or Parameter Store, CloudWatch, and SNS. Equivalent substitutions need an accepted ADR because they affect state ownership or delivery guarantees.

## Data flow

1. Publisher validates and promotes one immutable configuration/inventory release.
2. Watcher loads that release and conditionally fetches every enabled feed.
3. Items are bounded, parsed, normalized, and coalesced by canonical announcement URL.
4. Service aliases and risk rules are evaluated against title and summary.
5. Enabled environment profiles are grouped by route.
6. Each revision/service/risk/route/audience combination produces at most one candidate.
7. Candidate and `pending_queue` delivery state become durable.
8. Feed validators advance only after all work from the response is durable.
9. Dispatcher sends due requests to FIFO groups keyed by Slack destination.
10. Worker claims, paces, posts, and records a known or unknown outcome.
11. Reconciler repairs abandoned work and alarms expose stale or terminal states.

## Supported scale

The canonical deployment declares 100 accounts as metadata, 500 environments, 25 feeds, 20 Slack routes, 100 services, 100 profiles, 50 risk rules, and 300 delivery requests per hour globally and per destination. A deployment may raise the global schema value to 1,000 only after load evidence proves its queue, worker, DynamoDB, and destination pacing settings can sustain it.

## Current status

The architecture, contracts, examples, validators, and tests are present. Runtime and Terraform implementation are planned. Acceptance language distinguishes design validation from deployed evidence.
