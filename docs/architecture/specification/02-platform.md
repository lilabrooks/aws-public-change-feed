# 2. Platform and state model

## AWS resources

The planned `infra/central` root creates:

- A versioned S3 configuration bucket with immutable release, active-manifest, and short-lived raw-feed snapshot prefixes.
- A feed watcher Lambda and schedule.
- A DynamoDB source-state table for feed checkpoints and announcement records.
- A DynamoDB delivery table for candidates, outbox work, destination pacing, and delivery outcomes.
- An encrypted SQS FIFO queue and FIFO DLQ.
- An outbox dispatcher Lambda, Slack worker Lambda, and recovery reconciler Lambda.
- Secrets Manager secrets or SecureString parameters referenced by exact identifier.
- CloudWatch logs, metrics, dashboard, alarms, and an operational SNS topic.

Resource names derive from `deployment_id` and remain within AWS naming limits. The configuration bucket blocks public access, requires TLS, enables versioning, and uses server-side encryption. Queue encryption uses SQS-managed keys by default.

## Source-state table

Use on-demand capacity for the baseline. A single-table layout may use `PK` and `SK` with explicit item types.

### Feed item

- Key: `PK = FEED#<feed_name>`, `SK = STATE`.
- Fields: feed URL, ETag, Last-Modified, last attempt, last success, newest observed publication time, consecutive failures, last error class, lease owner, lease expiry, TTL.
- A conditional lease prevents overlapping fetches for the same feed.
- A `304 Not Modified` response updates success and freshness without source items.
- Validator changes use a conditional expression based on the lease and previously observed version.

### Announcement item

- Key: `PK = ANNOUNCEMENT#<announcement_id>`, `SK = STATE`.
- Fields: canonical URL, latest content fingerprint, known revision IDs, normalized title and summary, first and last observed times, optional source publication and update times, merged provenance, emitted candidate IDs, release references, TTL.
- Conditional update merges provenance and appends a revision only when absent.
- Raw content is excluded. A bounded raw response snapshot may live in S3 for replay during its configured retention.

## Delivery table

Use on-demand capacity for the baseline. The table needs a GSI named `status-next-action-index` whose partition key is `status` and sort key is `next_action_at`. Only work that needs scheduling has those attributes. GSI reads are eventually consistent, so the dispatcher can observe a short delay. The scheduled reconciler scans bounded overdue state through a separate recovery path and prevents an index delay from becoming permanent loss.

### Candidate item

- Key: `PK = CANDIDATE#<candidate_id>`, `SK = CANDIDATE`.
- Contains the validated candidate, immutable release references, first and last observed times, and terminal-history TTL.
- A conditional put makes repeated watcher execution idempotent.

### Delivery item

- Key: `PK = CANDIDATE#<candidate_id>`, `SK = DELIVERY`.
- Contains the exact request, destination key, state, state version, creation time, next action, dispatch generation and ID, queue message ID, attempt count, network-attempt count, lease, Slack response metadata, manual replay history, and TTL when safe.
- State transitions use conditions on current state, version, and lease.

The states are `pending_queue`, `queued`, `sending`, `posted`, `failed_retryable`, `failed_terminal`, and `delivery_unknown`. `posted` and `failed_terminal` may expire after the configured terminal retention. Unresolved states have no TTL or receive a retention extension. A new put over a terminal expired item must prove `expires_at < now` in its condition; DynamoDB TTL deletion is asynchronous.

### Destination item

- Key: `PK = DESTINATION#<destination_key>`, `SK = PACE`.
- Contains `next_allowed_at`, last response class, and a monotonic version.
- Conditional updates serialize pacing decisions across workers.

## Durable creation boundary

For each feed response, the watcher must make candidate and delivery records durable before saving the response's new ETag or Last-Modified value. Batch work may use transactions in bounded groups. When a response produces more records than one transaction can hold, record a response-run item and deterministic page markers. The feed checkpoint can advance only after every page marker is complete.

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

- Dispatches or signals overdue `pending_queue` records.
- Re-enqueues due retryable records when no valid queue claim remains.
- Marks expired `sending` leases as `delivery_unknown`.
- Detects `queued` records older than the maximum expected queue age.
- Extends unresolved state retention.
- Emits counts and oldest age for every nonterminal state.

The reconciler never automatically retries `delivery_unknown`.

## SQS configuration

Use a FIFO queue and FIFO DLQ. Set content-based deduplication off because the dispatch ID is explicit. The redrive threshold is `queue_max_receive_count`, which must exceed `max_network_attempts` and leave room for pacing and transient worker failures.

The visibility timeout must exceed Lambda timeout plus the maximum work duration for one batch. Long Slack delays belong in `next_action_at` and a later queue delivery; the worker does not sleep for them. Reserved concurrency and batch size are chosen from the load test and destination count.

## Scheduling and concurrency

- Feed watcher: every 10 to 15 minutes by default, with per-feed conditional leases.
- Dispatcher: event-driven where practical plus a scheduled recovery scan.
- Slack worker: SQS event source, reserved concurrency from deployment configuration.
- Reconciler: every five minutes.

Schedules use retries and DLQs or equivalent failure destinations. Heartbeat alarms distinguish a quiet source from a scheduler failure.

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
