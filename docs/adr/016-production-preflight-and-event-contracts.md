# ADR-016: Production preflight and event contracts

- Status: Accepted
- Date: 2026-07-12

## Context

Architecture prose does not prove that a deployment can fetch every feed, resolve every route, render every candidate, or deliver at its declared rate. Producer and worker boundaries also require strict payload contracts.

## Decision

Use JSON Schema draft 2020-12 contract version 3 for `AlertCandidate` and `DeliveryRequest`. Both reject unknown and credential-bearing fields. A delivery request embeds the exact candidate and uses a deterministic request ID. The feed watcher validates a candidate before its outbox write. The dispatcher validates the request before SQS. The worker validates it again before state claims or secret reads.

Before production promotion, record evidence that:

- Every feed passes approved-host, DNS, TLS, response-size, parsing, and freshness checks.
- A historical announcement corpus meets reviewed precision and recall thresholds for each enabled service and risk type.
- Inventory, profiles, routes, and Slack destinations resolve exactly.
- Webhook or bot credentials can post a synthetic message to each destination through the deployed worker role.
- Queue visibility, reserved concurrency, destination pacing, and measured request duration support the declared delivery envelope.
- DynamoDB indexes, TTL policies, alarms, DLQ subscription, scheduler heartbeats, and reconciler recovery tests pass.
- Terraform backend permissions include exact state and lockfile object access, prefix-limited bucket listing, and KMS access when used.

Production readiness is a two-step confirmation for operational alarm delivery: infrastructure validation proves topic policy and subscription configuration; an operator then confirms the test notification was received.

## Consequences

- Boundary failures are caught before credentials or external calls.
- Production claims have deploy-time evidence.
- Contract fixtures become replay and compatibility tests.

## References

References verified: 2026-07-13.

- [JSON Schema draft 2020-12](https://json-schema.org/draft/2020-12)
- [AWS Lambda best practices](https://docs.aws.amazon.com/lambda/latest/dg/best-practices.html)
- [Terraform S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3)
- [SNS subscription confirmation](https://docs.aws.amazon.com/sns/latest/dg/SendMessageToHttp.confirm.html)
