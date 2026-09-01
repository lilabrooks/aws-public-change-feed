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

- Status: Accepted
- Date: 2026-08-10
- Accepted: 2026-08-10

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

## Revision: Lambda timeout is the worker capacity bound

- Status: Accepted
- Date: 2026-08-10
- Accepted: 2026-08-10

This revision replaces the preceding visibility calculation and resolves the
capacity question left by the worker-transport revision.

The configured Lambda timeout is the hard outer bound for one worker invocation.
The FIFO queue visibility timeout is at least six times that function timeout,
plus any configured batch window. With the canonical 300-second worker timeout,
the minimum queue visibility is therefore 1,800 seconds. The earlier 420-second
calculation does not satisfy this rule and must be replaced before the event
source mapping is created.

Before starting each record, the FIFO handler reads the Lambda context's remaining
time. When its configured safety reserve is unavailable, it stops and returns the
current and every unprocessed record in `batchItemFailures`. After any record
failure it does the same, preserving FIFO order. Successfully handled records stay
acknowledged through the partial-batch response.

The Slack transport timeout covers each blocking socket operation separately;
DNS, connect, write, and read remain separate phases. If Lambda reaches its hard timeout after a sending claim,
the request may have reached Slack. The record remains `sending` until the
reconciler converts the expired lease to `delivery_unknown`; automatic retry is
still forbidden.

Revisit the capacity choice before increasing worker batch size or function
timeout, after any Lambda timeout produces `delivery_unknown`, when measured p99
batch duration exceeds half the configured function timeout, or when bounded DNS
and one monotonic request deadline can be added without weakening the shared
anti-rebinding controls.

## Revision: recovery is bounded and acts only on proven-safe state

- Status: Accepted
- Date: 2026-08-10
- Accepted: 2026-08-10

The recovery reconciler runs every five minutes with a 60-second timeout,
reserved concurrency of one, and a limit of 100 repairs per invocation. It
observes at most 101 records per state, reports the first 100, and emits a fixed
`StateObservationSaturated` metric when more exist. Reaching a repair or
observation cap, p99 runtime above 30 seconds, overlapping scheduled work, or a
larger declared scale envelope reopens these values.

Due `pending_queue` and `failed_retryable` records use the existing dispatcher
path. That path owns exact-request validation, the dispatch claim, FIFO group,
deduplication ID, queue send, and `queued` transition. Recovery does not build a
second queue protocol.

An expired `sending` lease becomes `delivery_unknown` only through a conditional
write that still owns the observed state version and attempt ID. The durable
`lease_expires_at` value is the authority even when initialization made the
lease outlive the Lambda invocation. The transition preserves network-attempt
and response evidence, writes no TTL, and never authorizes automatic replay.
Revisit the unknown outcome after material false-unknown evidence, an audited
manual-replay path, a durable pre-network phase that does not create another
crash gap, or a reviewed Slack idempotency or outcome-query mechanism.

An old `queued` record is evidence of a queue or worker problem, not proof that
SQS lost the message. The canonical stale threshold is the existing 600-second
queue-age threshold. Because SQS supplies no authoritative lookup by message ID,
recovery emits age and stale-work signals but does not enqueue it again.
`posted`, `failed_terminal`, and `delivery_unknown` receive no automatic
transition.

Unresolved records preserve retention by having no TTL. Recovery keeps applying
that invariant to records it reads and transitions; it does not write periodic
TTL extensions or scan the whole table to restate it.

The reconciler uses its own exact package digest and S3 object-version inputs so
its rollout cannot silently update the Slack worker. Scheduled target failures
receive at most two EventBridge retries while the event is no more than 300
seconds old, then enter a separate encrypted standard runtime-failure queue.
That queue is distinct from the delivery FIFO DLQ and permits sends only from
the exact schedule rule.

## Revision: expired sending remains an unknown outcome

- Status: Accepted
- Date: 2026-08-31
- Accepted: 2026-08-31

The post-MVP reassessment trigger is satisfied. The isolated recovery exercise
moved one source-built expired `sending` record to `delivery_unknown` with zero
Slack network attempts. The found-post and unknown-replay actions now provide
preview-first, conditional, audited operator paths for both conclusive review
outcomes.

The controlled record was created with an expired lease and no Slack attempt. It
proves that recovery makes the conditional state transition without another
network call. It is not a sample of a real worker failure after a write may have
started, so it does not establish the false-unknown base rate or prove that a
naturally expired Slack attempt sent no bytes.

Three courses were considered. Automatically retrying every expired lease could
repeat a post that Slack already accepted. Inferring safety from an absent
response or a stored zero network-attempt count would trust evidence that a hard
worker stop may not have persisted. Retaining `delivery_unknown` keeps the
uncertainty explicit while using the audited operator paths to resolve it.

The service therefore continues to convert an expired `sending` lease to
`delivery_unknown` through the existing exact conditional write and never
automatically retries that record. An operator who finds the original Slack post
uses found-post closure. An operator who completes a conclusive search without a
match may reserve one audited replay. An inconclusive search leaves the record
unchanged. No stored-data migration, runtime change, or configuration release is
required.

Reopen this decision only when checked field evidence distinguishes material
false unknowns that are safe to retry, a durable pre-network phase closes the
crash gap without creating another one, or Slack supplies a reviewed idempotency
or outcome-query mechanism.

## Consequences

- Feed checkpoints cannot pass undurable delivery work.
- Slack secrets are limited to one runtime role.
- Queue loss or delayed dispatch is recoverable from DynamoDB.
- Ready-work ordering, scheduled-retry overtaking, and destination pacing are explicit.

## References

References verified: 2026-08-10.

- [Lambda with SQS](https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html)
- [SQS FIFO delivery logic](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/FIFO-queues-understanding-logic.html)
- [Lambda partial batch responses](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html)
- [SQS message deduplication IDs](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/using-messagededuplicationid-property.html)
- [Secrets Manager with Lambda](https://docs.aws.amazon.com/secretsmanager/latest/userguide/retrieving-secrets_lambda.html)
- [Configure an SQS event source mapping](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html)
- [Lambda Python context](https://docs.aws.amazon.com/lambda/latest/dg/python-context.html)
