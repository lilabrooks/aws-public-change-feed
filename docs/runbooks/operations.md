# Operations runbook

## Scope

This runbook covers the production path from public feed acquisition through Slack outcome state. DynamoDB candidate and delivery records are the operational source of truth. Slack history is supporting evidence for ambiguous outcomes.

Record the deployment ID, AWS account, Region, dashboard, operational SNS topic, configuration bucket, source table, delivery table, queue, DLQ, and current on-call owner in the deployed runbook header.

## First response

1. Confirm the alarm, deployment, Region, component, and first failure time.
2. Check component heartbeat, recent deployment and release changes, and the oldest affected state.
3. Preserve candidate IDs, request IDs, dispatch IDs and generations, feed names, response classes, attempt IDs, and release IDs. Do not copy secrets, webhook URLs, feed bodies, or full Slack payloads into tickets.
4. Decide whether the fault is acquisition, matching, durable emission, dispatch, queue processing, Slack delivery, or observability.
5. Stop automatic actions only at the smallest safe boundary. Prefer disabling one feed, route, or event source mapping over the whole service.
6. Use conditional state transitions and audited replay tools. Never edit delivery records by hand.

## Feed stale or fetch failing

1. Identify the feed and compare last attempt, last success, newest observed publication time, ETag, Last-Modified, and error class.
2. Check the scheduler and watcher heartbeat. If all feeds are stale, inspect the shared runtime, DNS, egress, release load, and source-table errors first.
3. For one feed, verify the configured URL and approved host against the publisher's official feed page.
4. Inspect safe diagnostics for DNS classification, TLS, content type, redirect rejection, response size, timeout, parser limit, and raw-snapshot status.
5. Do not relax the host allowlist, address checks, TLS, redirect, or parser limits during incident response. Add a reviewed configuration or code change with a regression fixture.
6. Run a shadow fetch using the deployed network path. It must not update validators or create candidates.
7. Restore service, confirm last success advances, and inspect the next normalized item count. Keep the prior ETag and Last-Modified until processing is durable.

## Feed appears quiet

1. Compare fetch success with newest observed publication age.
2. Check the official feed in a safe read-only browser or shadow fetch.
3. If the feed is genuinely quiet, document the evidence and tune only the publication-age expectation for that feed.
4. If new official items are absent from parsing, retain the raw snapshot and open a parser regression using a minimized safe fixture.

## Match quality problem

### False positive

1. Locate the candidate, revision, matched service alias, risk terms, fields, profile, and release.
2. Add the announcement to the historical corpus as a negative case.
3. Change aliases, `any`/`all`/`none` terms, or boundary behavior in a new release.
4. Run the full corpus and record per-service and per-risk precision and recall.
5. Promote only when approved thresholds and existing positives still pass.

### Missed announcement

1. Confirm the item was fetched and normalized. Distinguish source failure from match failure.
2. Check canonical URL, title, summary, service aliases, risk terms, exclusions, profile membership, and environment policy.
3. Add the announcement as a positive corpus fixture and a nearby hard negative.
4. Test the candidate and route output in shadow mode before promotion.

Rule changes do not rewrite historical candidates. If an operator needs a past item re-evaluated, use the audited snapshot replay tool with a chosen immutable release.

## Candidate or outbox gap

1. Find the feed response-run marker and expected deterministic candidate IDs.
2. Confirm whether candidate and delivery items exist and whether the feed validator advanced.
3. If the validator did not advance, rerun normal feed processing. Conditional writes should fill missing records.
4. If the validator advanced without every required outbox record, treat it as a correctness incident. Pause the watcher, preserve state and snapshots, and repair through a reviewed recovery tool.
5. Do not synthesize a new candidate ID. Recompute from the exact source revision and release.
6. After repair, verify response-run completion and resume the feed lease.

## Outbox or queue backlog

1. Compare oldest `pending_queue`, `queued`, and `failed_retryable` ages with SQS visible, in-flight, and oldest-message metrics.
2. Check dispatcher and reconciler heartbeat, delivery-table throttles, index status, worker errors, reserved concurrency, and destination pacing.
3. If DynamoDB has pending work and SQS is empty, inspect dispatcher sends and state-update conditions.
4. If SQS has work and the worker is idle, inspect the event source mapping, permissions, concurrency, and visibility timeout.
5. If one destination is delayed, inspect its pacing record and recent `Retry-After`; keep other FIFO groups running.
6. Scale only within the validated support envelope. Slack destination pacing may remain the limiting factor after Lambda capacity increases.
7. Confirm backlog age falls and no new unknown outcomes appear.

## Slack retryable or terminal failure

1. Locate the delivery record by request or candidate ID and inspect response class, network-attempt count, next action, and destination key.
2. For rate limiting, validate the bounded `Retry-After` and destination pacing record. Do not force immediate retry.
3. For server errors, allow the reviewed backoff policy to continue until its network-attempt limit.
4. For a terminal credential, hook, workspace, channel, or payload error, disable only the affected route if messages would otherwise accumulate without value.
5. Correct the secret or configuration through its normal reviewed path and run a synthetic destination preflight.
6. Use an audited replay action for terminal records that require delivery after correction.

## Delivery unknown

Automatic retry is forbidden because Slack may already contain the message.

1. Record the candidate ID, request ID, attempt ID, destination, request start time, lease expiry, and worker invocation.
2. Search the destination around that time using title, source link, and compact candidate ID. Respect customer access boundaries.
3. If the message exists, use the audited reconciliation action to mark `posted` and record its Slack identifier when available.
4. If the message is absent and the operator accepts the remaining duplication risk, create a manual replay with operator, reason, evidence, prior attempt, and new attempt.
5. If evidence is inconclusive, leave the state unknown and escalate to the service owner.
6. Review timeout, lease, visibility, and worker termination evidence before closing the incident.

## DLQ response

1. Stop bulk redrive.
2. Inspect a sample and map each request ID to its delivery record and exact release.
3. Separate poison payloads, incompatible contracts, permission failures, pacing redeliveries, worker faults, and state-transition conflicts.
4. Fix and test the cause with a regression fixture.
5. Redrive a small batch. Confirm no duplicate network call occurs for `posted` or active `sending` records.
6. Expand redrive gradually while watching queue age, unknown outcomes, and terminal failures.

## Release failure or rollback

1. Identify the active manifest S3 version and the last known good immutable release.
2. Verify object keys, object version IDs, hashes, schema versions, and application compatibility.
3. Stop promotion if compare-and-swap failed; another publisher changed the active pointer.
4. Promote the retained prior manifest through the normal conditional process.
5. Run a read-only load and shadow match probe.
6. Confirm watcher, dispatcher, and worker can still load historical releases referenced by in-flight delivery records.
7. Record the failed and restored release IDs.

## Manual source replay

1. Name the retained raw snapshot or bounded time range, target release, purpose, operator, and expected route scope.
2. Run dry mode first and compare candidate IDs with existing candidate history.
3. By default, replay fills missing state and suppresses existing candidates.
4. Any request to resend an existing candidate uses the manual delivery replay path and its audit fields.
5. Keep feed validators unchanged during snapshot replay.

## Alarm delivery failure

1. Verify topic policy, subscription state, delivery status logging, and the receiving endpoint or email confirmation.
2. Send the approved synthetic operational notification.
3. Infrastructure checks can prove configuration. An operator records receipt before production readiness is restored.
4. Maintain an alternate escalation path while operational notifications are impaired.

## Security incident

### Suspected Slack credential exposure

1. Disable the affected route or worker event source mapping.
2. Rotate the exact secret in Slack and the configured secret store.
3. Review secret access logs and worker logs without printing the value.
4. Run a synthetic route preflight and resume delivery.
5. Audit queued and unknown records before replay.

### Suspected unsafe fetch or source-content exploit

1. Disable the affected feed.
2. Preserve safe metadata and the retained snapshot under incident access controls.
3. Review DNS results, redirect behavior, response limits, parser events, and rendered message output.
4. Patch the control and add regression fixtures before reenabling the source.

## Capacity change

1. Update the declared envelope, destinations, pacing, Lambda batch and concurrency, queue visibility and receives, and table assumptions together.
2. Run the production-like load test with destination distribution that reflects expected bursts.
3. Record duration, throttles, queue age, outbox age, rate responses, DLQ, and unknown outcomes.
4. Promote only after semantic validation and the acceptance thresholds pass.

## Closure evidence

Close an incident when the root cause and affected range are known, state is reconciled, backlog and freshness recover, no unexpected route crossing or duplicate network calls occurred, alarms and heartbeats are healthy, and follow-up tests or configuration changes are linked.

## References

References verified: 2026-07-13.

- [CloudWatch alarm troubleshooting](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/troubleshooting-alarms.html)
- [SQS DLQ redrive](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)
- [Slack incoming webhooks](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/)
- [DynamoDB condition expressions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html)
