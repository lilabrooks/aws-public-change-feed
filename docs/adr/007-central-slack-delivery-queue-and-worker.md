# ADR-007: Durable outbox and Slack worker

- Status: Accepted
- Date: 2026-07-12

## Context

Advancing feed validators before delivery work is durable can lose alerts. Calling Slack from the feed watcher also mixes source processing with credentials, rate control, and HTTP ambiguity.

## Decision

The feed watcher writes each validated candidate and its `pending_queue` delivery record before it advances feed validators. A dispatcher queries the delivery table's `status-next-action-index`, sends the exact `DeliveryRequest` to an encrypted SQS FIFO queue, then marks the record `queued`.

Use `destination_key` as `MessageGroupId`. Each intentional queue delivery has a monotonically increasing `dispatch_generation` in its DynamoDB delivery record and:

```text
dispatch_id = SHA256(
  "queue-dispatch:v1\0" + request_id + "\0" + decimal_dispatch_generation
)
```

Use `dispatch_id` as `MessageDeduplicationId`. A dispatcher claims a generation conditionally before calling SQS. It reuses that generation and ID if the SQS outcome or following state update is uncertain. The worker clears the active dispatch claim when it schedules a future retry, so the next due dispatch gets a new generation. This prevents SQS's five-minute deduplication window from discarding an intentional retry while still suppressing duplicate sends of one dispatch attempt.

FIFO ordering provides one ordered stream of ready queue deliveries per Slack destination. A retry moved to a future `next_action_at` may be delivered after newer ready work for that destination. DynamoDB remains the durable delivery system of record because SQS deduplication is time-limited.

The `slack-delivery-worker` is the only component that reads Slack credentials or performs Slack HTTP requests. It validates the request, verifies its embedded candidate and immutable release, claims the delivery record conditionally, and follows ADR-004 and ADR-015. A reconciler repairs dispatchable records and converts expired `sending` leases to `delivery_unknown`.

Use SQS-managed encryption by default, partial batch responses, a DLQ, and a visibility timeout derived from Lambda timeout plus the maximum in-invocation batch work and a safety margin. Scheduled rate delays do not extend an invocation. Customer-managed KMS requires exact producer and consumer grants.

## Revision: the worker hands the transport a typed destination

- Status: Proposed
- Date: 2026-08-10

The worker is the only component that reads Slack credentials or performs Slack
HTTP requests, but the port it did that through could not express bot-mode
delivery: a token is usable against any channel in its workspace, and nothing
carried the release route's channel to the transport. Passing the rendered
payload as the routing source would have made a candidate able to choose its own
channel, and reading the mode from the credential would have taken it from
mutable deployment state rather than from the verified release.

`SlackSender.post` therefore takes a typed destination derived inside the worker
from the exact inventory release. It carries the delivery mode, the channel ID in
bot mode, and the approved webhook hosts in webhook mode. Its mode-specific
invariants hold at construction, and a route that cannot produce one is a
terminal delivery with no network call.

The transport reports observed facts and no delivery state. It may claim that no
request byte was sent only where that is provable, which requires connecting
explicitly rather than letting the handshake happen inside the first write; the
`Retry-After` it reports is the integer received, because bounding it is release
policy the transport never sees.

Both accepted `secret_store` values get an adapter. Which one a deployment uses
is a composition-root choice, so neither the credential port nor the worker names
a store, and the stored content is the whole credential.

A credential read resolves to a delivery state by whether another identical read
could succeed. Permanent conditions — absent, denied, missing, empty, binary, an
SSM parameter that is not a `SecureString`, or a successful response whose shape
is unusable — are configuration corrections and resolve terminally. Transient
conditions — throttling, an outage, an internal provider failure, a connection
failure, a read timeout — reschedule the record with a bounded delay, make no
Slack call, leave the network-attempt budget unchanged, and do not advance
destination pacing, because that destination was never called. The permanent set
is an explicit allowlist and everything else is transient: a bounded retry is
recoverable, and a terminally discarded alert is not.

The configured kind and the stored content are two different checks. The kind
records the mode the reader was built for and so detects a release-versus-wiring
mismatch; it cannot establish what an operator actually stored. One shared content
validator does that for bot tokens, called before the network-attempt counter
moves and again at the network boundary.

A received Slack status decides the outcome, and a body is read only where the
status does not decide by itself. Reading first let a slow or oversized body
replace a definite answer with a transport ambiguity, which turned an accepted
webhook post into work an operator had to reconcile by hand.

Address validation and the pinned TLS connection are shared with feed
acquisition rather than reimplemented, so chapter 04's "the same anti-rebinding
controls" is a property of one implementation instead of a claim about two.

One capacity question is deliberately left open. `slack_request_timeout_seconds`
currently reaches blocking socket operations separately and does not prove a
complete wall-clock request deadline, because DNS resolution and several blocking
phases each carry their own bound. Before the FIFO Lambda handler, its event
source mapping, and the timeout-derived visibility and capacity figures are called
complete, this repository must either enforce an end-to-end monotonic deadline
including bounded DNS, or replace the capacity calculation with a provable upper
bound. Until then no text here claims the adapter bounds a request's total
duration.

## Consequences

- Feed checkpoints cannot pass undurable delivery work.
- Slack secrets are limited to one runtime role.
- Queue loss or delayed dispatch is recoverable from DynamoDB.
- Ready-work ordering, scheduled-retry overtaking, and destination pacing are explicit.

## References

References verified: 2026-07-13.

- [Lambda with SQS](https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html)
- [SQS FIFO delivery logic](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/FIFO-queues-understanding-logic.html)
- [Lambda partial batch responses](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html)
- [SQS message deduplication IDs](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/using-messagededuplicationid-property.html)
- [Secrets Manager with Lambda](https://docs.aws.amazon.com/secretsmanager/latest/userguide/retrieving-secrets_lambda.html)
