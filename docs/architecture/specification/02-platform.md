# 2. Platform and state model

## AWS resources

The `infra/central` root creates:

- A versioned S3 configuration bucket with immutable release, active-manifest, and short-lived raw-feed snapshot prefixes.
- A conditional feed watcher Lambda and schedule when its exact package pair is supplied.
- A DynamoDB source-state table for feed checkpoints and announcement records.
- A DynamoDB delivery table for candidates, outbox work, destination pacing, and delivery outcomes.
- An encrypted SQS FIFO queue and FIFO DLQ.
- Conditional outbox dispatcher, Slack worker, and recovery reconciler Lambdas, plus their least-privilege roles.
- Secrets Manager secrets or SecureString parameters referenced by exact identifier.
- CloudWatch logs, metrics, dashboard, alarms, and an operational SNS topic.

Resource names derive from `deployment_id` and remain within AWS naming limits. The configuration bucket blocks public access, requires TLS, enables versioning, and uses server-side encryption. Queue encryption uses SQS-managed keys by default.

## Source-state table

Use on-demand capacity for the baseline. A single-table layout may use `PK` and `SK` with explicit item types.

### Feed item

- Key: `PK = FEED#<feed_name>`, `SK = STATE`.
- Fields: feed URL, ETag, Last-Modified, first and last attempt, last success, newest observed publication time, consecutive failures, last error class, pending validators and response-run ID, lease owner, lease expiry, and monotonic state version.
- A conditional lease prevents overlapping fetches for the same feed. The canonical lease is 360 seconds, carries an opaque invocation owner, and may be replaced only after expiry.
- A `304 Not Modified` response updates success and freshness without source items.
- Fetched validators remain pending until downstream durability is proved. Attempt, failure, pending, `304`, and final writes require the exact lease owner and state version. The final fetched-feed commits use one transaction, so a lost condition advances none of the batch.
- Active feed items have no cleanup TTL. Removed-feed retirement needs a separate policy.

### Announcement item

- Key: `PK = ANNOUNCEMENT#<announcement_id>`, `SK = STATE`.
- Fields: canonical URL, current content fingerprint and revision, known revision IDs, normalized title and summary, first, last, and current-content observation times, optional source publication time, merged provenance, emitted candidate IDs, release references, and monotonic state version.
- Conditional compare-and-swap merges provenance, revision history, emission references, and release references without losing another writer. The sighting with the later `observed_at` owns current content; equal times choose the lexically smaller revision ID.
- Raw content is excluded. A bounded raw response snapshot may live in S3 for replay during its configured retention.

## Delivery table

Use on-demand capacity for the baseline. The table needs a GSI named `status-next-action-index` whose partition key is `status` and sort key is `next_action_at`. Both attributes are present on work the dispatcher must discover. `next_action_at` is a DynamoDB Number containing whole Unix epoch seconds; a new `pending_queue` record sets it to its creation time so the initial dispatch is immediately due. GSI reads are eventually consistent, so the dispatcher can observe a short delay. The scheduled reconciler scans bounded overdue state through a separate recovery path and prevents an index delay from becoming permanent loss.

### Candidate item

- Key: `PK = CANDIDATE#<candidate_id>`, `SK = CANDIDATE`.
- Contains the validated candidate, immutable release references, first and last observed times, and terminal-history TTL.
- A conditional put makes repeated watcher execution idempotent.

### Delivery item

- Key: `PK = CANDIDATE#<candidate_id>`, `SK = DELIVERY`.
- Contains the exact request, destination key, state, state version, creation time, numeric next action, dispatch generation and ID, queue message ID, active, last, and reserved-next attempt IDs, network-attempt count, lease, Slack response metadata, unknown-replay history, terminal-replay history, found-post history, and TTL when safe.
- State transitions use conditions on current state, version, and lease.

The states are `pending_queue`, `queued`, `sending`, `posted`, `failed_retryable`, `failed_terminal`, and `delivery_unknown`. `posted` and `failed_terminal` may expire after the configured terminal retention. Unresolved states have no TTL or receive a retention extension. A new put over a terminal expired item must prove `expires_at < now` in its condition; DynamoDB TTL deletion is asynchronous.

A completed sending claim copies its active attempt ID to `last_attempt_id`
before clearing the lease. An operator-approved replay of `delivery_unknown`
appends a bounded history entry with decision time, operator, reason, evidence,
prior attempt, and new attempt. The same conditional write proves the observed
unknown state version and prior attempt, reserves the new attempt, and returns
the record to immediately due `pending_queue`. The dispatcher preserves that
reservation through `queued`. If destination pacing defers the record, the
reservation remains on `failed_retryable` through the later redispatch. The
worker consumes it into the next `sending` claim before another Slack call. If
the operator instead finds the original Slack post, a separate conditional
write appends bounded found-post evidence, records `posted`, applies terminal
retention, and reserves no new attempt. A stale decision writes nothing.

After an operator verifies an exact-request-compatible correction, a separate
terminal replay may move one live `failed_terminal` record to immediately due
`pending_queue`. Its dedicated history entry retains decision time, operator,
reason, evidence, prior and reserved attempt IDs, prior bounded response class,
prior exhausted-budget flag, and prior expiry. The conditional write proves the
exact state version, prior attempt, still-live and unchanged terminal expiry,
unchanged bounded outcome, absent reservation, and available 25-entry history
capacity before removing expiry and reserving one attempt. It preserves the
stored request, candidate, destination, release, application digest, dispatch
generation, creation time, and historical network-attempt count.

### Destination item

- Key: `PK = DESTINATION#<destination_key>`, `SK = PACE`.
- Contains `next_allowed_at`, last response class, and a monotonic version.
- Conditional updates serialize pacing decisions across workers.

## Durable creation boundary

For each feed response, the watcher must make candidate and delivery records durable before saving the response's new ETag or Last-Modified value. A response-run ID is the null-framed SHA-256 of `feed-response-run:v1`, feed name, body SHA-256, release ID, and application digest. Because cross-feed coalescing can give the same response body a different candidate set when another feed succeeds or fails, each immutable page set also has a null-framed SHA-256 identity over `feed-response-pages:v1`, the response-run ID, and every sorted candidate ID. Candidate IDs are sorted into pages of at most 25 within that set; page numbers start at zero, and a zero-candidate response has one empty completion page. A marker is written only after durable read-back. The feed checkpoint advances only after every current page and final candidate, delivery, emission, and release reference is read back.

A repeated invocation can safely reconstruct the same candidates and conditionally put missing records. Candidate identity is deterministic, so partial completion cannot create new logical work.

## Outbox dispatcher

The dispatcher queries `pending_queue` and eligible `failed_retryable` items whose `next_action_at` is due. It validates each stored request and conditionally claims a queue dispatch generation. The dispatch ID is:

```text
SHA256("queue-dispatch:v1\0" + request_id + "\0" + decimal_dispatch_generation)
```

The decimal generation has no sign or leading zero. The dispatcher sends the request to SQS FIFO and conditionally moves it to `queued` with the returned SQS message ID.

Use:

- `MessageGroupId = destination_key`
- `MessageDeduplicationId = dispatch_id`

If the send result is unknown or the following state update fails, leave the claimed generation on the delivery record and reuse its dispatch ID. FIFO dedupe suppresses duplicate sends of that queue attempt; the worker's DynamoDB claim handles the lasting case. When the worker schedules a future retry, it clears the active dispatch claim. The next due dispatch conditionally increments the generation and uses a new dispatch ID so SQS does not suppress valid retry work during its deduplication window.

The canonical regular dispatcher runs every minute with a 60-second function
timeout, reserved concurrency one, and a 100-record cap across both due states.
Its EventBridge target gets two retries while the event is no more than 300
seconds old. Exhausted events enter the encrypted standard runtime-failure
queue under a policy scoped to the exact dispatcher schedule ARN and account.

## Slack worker

The Lambda event source mapping uses partial batch responses. For a FIFO batch, processing stops after the first failed record, and the response lists that record plus every record not yet processed in the batch. This follows the Lambda FIFO partial-batch rule and preserves queue ordering. The worker:

1. Validates the delivery request.
2. Confirms the embedded candidate and request IDs.
3. Loads and verifies the exact immutable release.
4. Reads destination pacing state.
5. Claims `queued` or an eligible retry as `sending` with a lease and attempt ID.
6. Reads the exact route credential.
7. Renders bounded plain-text-safe blocks and fallback text.
8. Performs at most one Slack network call for that claim.
9. Records `posted`, `failed_retryable`, `failed_terminal`, or `delivery_unknown`.

An SQS redelivery for `posted` is acknowledged. A message whose record still has the matching pending dispatch claim is returned unprocessed; the dispatcher reuses that dispatch ID and completes the `queued` transition. A message for another active `sending` lease is also returned unprocessed without a network call. When the worker records a future `failed_retryable` action, it acknowledges the current SQS message; the dispatcher creates a new queue delivery after `next_action_at`. That delayed retry may follow newer ready work in the same destination group. SQS receive count, dispatch generation, and Slack network-attempt count remain separate.

## Recovery reconciler

Run at least every five minutes. It:

- Sends due `pending_queue` and `failed_retryable` records through the existing
  dispatcher path and its dispatch-claim protocol.
- Marks an expired `sending` lease as `delivery_unknown` only while a
  conditional write still owns the observed state version and attempt ID.
- Detects `queued` records at least 600 seconds old and emits age and stale-work
  signals without enqueuing them again, because queue age is not proof that SQS
  lost a message.
- Preserves unresolved evidence by keeping TTL absent on every unresolved
  record it reads or transitions; it performs no periodic TTL-extension write.
- Observes bounded counts and oldest age for automatically actionable states,
  with an explicit saturation signal when the observation cap is exceeded.

The reconciler never automatically retries `delivery_unknown`.

The canonical reconciler runs every five minutes with a 60-second function
timeout, reserved concurrency one, and at most 100 repairs per invocation. It
reads at most 101 records per observed state, reports the first 100, and emits
`StateObservationSaturated` when more exist. Its scheduled target gets two
retries within a 300-second maximum event age before the event enters the
separate standard runtime-failure queue.

## SQS configuration

Use a FIFO queue and FIFO DLQ. Set content-based deduplication off because the dispatch ID is explicit. The redrive threshold is `queue_max_receive_count`, which must exceed `max_network_attempts` and leave room for pacing and transient worker failures.

Delivery-DLQ recovery uses the native SQS message-movement task. The operator
tool verifies that both queues are FIFO, the source queue's redrive policy names
the supplied DLQ, and the DLQ's `byQueue` allow policy names only that source.
It leaves the task destination unset so SQS returns messages to their original
source queue. Only one task may be active for the DLQ. Start and cancellation
are explicit mutations; preview and status inspect queue attributes and recent
task state only.

The operator supplies a fixed movement velocity from 1 through the SQS maximum
of 500 messages per second. Task and queue counts are approximate. Native
movement does not impose an exact message-count boundary: messages entering the
DLQ while a task runs can join it, cancellation can lag, and redriven messages
can interleave with new source-queue traffic. SQS assigns redriven work new
message IDs and enqueue times. The tool does not receive, rewrite, send, or
delete individual messages, and DynamoDB remains the delivery authority.

The visibility timeout must exceed Lambda timeout plus the maximum work duration for one batch. Long Slack delays belong in `next_action_at` and a later queue delivery; the worker does not sleep for them. Reserved concurrency and batch size are chosen from the load test and destination count.

## Scheduling and concurrency

- Feed watcher: every 15 minutes, with a 300-second timeout, reserved concurrency one, deployment-controlled fetch concurrency, and 360-second per-feed leases. It stops claiming new feeds below a 60-second remaining-time reserve.
- Dispatcher: every minute, with a 60-second timeout, reserved concurrency one, and a 100-record invocation cap.
- Slack worker: SQS event source, reserved concurrency from deployment configuration.
- Reconciler: every five minutes.

The watcher target receives at most two retries while its event is no more than 900 seconds old. The dispatcher target receives at most two retries while its event is no more than 300 seconds old. Exhausted events enter the encrypted standard runtime-failure queue under policies scoped to each exact schedule ARN and the current account. Heartbeat alarms distinguish a quiet or empty pass from a scheduler failure.

## Terraform state

The bootstrap root creates a private, versioned state bucket. The service root uses native S3 locking with Terraform `>= 1.10.0, < 2.0.0`. Backend IAM includes exact object permissions for state and `.tflock`, prefix-limited `s3:ListBucket`, and exact KMS permissions when applicable. Every root commits its provider lock file after validation on the supported platforms.

## References

References verified: 2026-07-13.

- [DynamoDB single-table design](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-modeling-nosql-B.html)
- [DynamoDB condition expressions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html)
- [DynamoDB TTL](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html)
- [SQS FIFO queues](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-fifo-queues.html)
- [SQS message deduplication IDs](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/using-messagededuplicationid-property.html)
- [Lambda SQS error handling](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html)
- [Terraform S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3)
