# 6. Acceptance and implementation sequence

## Contract acceptance

- Given a clean checkout, the six canonical examples load as one bundle, every document passes its paired JSON Schema, and all cross-document semantic checks pass.
- Given documents that pass their schemas individually but disagree on projections, references, routes, or release data, bundle validation fails.
- Given an example edit that changes release or event identity inputs without recalculating dependent hashes, bundle validation fails.
- Given an unknown field in any owned object, validation fails.
- Given a mismatched release hash, version, key, route, environment policy, profile, service, alias, destination, or identity field, validation fails.
- Given a rejected configuration or event mutation, a regression test proves the rejection.
- Given an older contract version, runtime either has an explicit compatible reader or rejects it before state changes and secret reads.

## Feed acquisition acceptance

- Given an unapproved scheme, host, port, credential-bearing URL, private or special IP, DNS rebind, redirect, invalid certificate, oversized body, unsupported content type, entity expansion, excessive items, excessive characters, or timeout, the fetch fails closed and does not advance validators.
- Given a valid `304`, last success advances and no announcement work is created.
- Given one failed feed and three successful feeds, successful items continue and the failed feed keeps its prior checkpoint.
- Given overlapping feeds containing one canonical URL, one announcement is evaluated with merged sorted provenance.
- Given a feed response that creates candidates, ETag and Last-Modified advance only after all candidate and outbox records are durable.
- Given a crash after partial durable writes, replay fills missing records without changing logical IDs or duplicating posted work.

## Matching acceptance

- Given service evidence and distinct positive risk evidence, the configured rule can match.
- Given only a service alias, only a risk term, an excluded term, overlapping evidence, or a substring without phrase boundaries, no candidate is emitted.
- Given a profile without the service, its environment is absent.
- Given a disabled environment, it is absent and its reason remains available for configuration review.
- Given two routes, each candidate contains only that route's sorted environment IDs.
- Given a title or summary edit, a new revision and candidate ID can result.
- Given only added provenance or corrected publication time, no new candidate is delivered.
- Given the historical corpus, recorded per-service and per-risk precision and recall meet the approved thresholds.

## Candidate and outbox acceptance

- Given the same normalized inputs and release, repeated runs produce byte-equivalent identity fields.
- Given a rule ID rename with the same risk type and evidence, candidate identity remains unchanged.
- Given a risk type, service, revision, or route change, candidate identity changes.
- Given a changed matching environment set, audience fingerprint and candidate identity change.
- Given a candidate over its byte limit, no dispatchable delivery is created and the failure is visible.
- Given an existing candidate or delivery record, conditional writes preserve the prior immutable content.
- Given an expired terminal item that DynamoDB has not deleted, replacement succeeds only when the condition proves its TTL is in the past.
- Given unresolved work, TTL cannot delete the evidence needed for recovery.

## Dispatch and queue acceptance

- Given due `pending_queue` work, the dispatcher sends an exact version 3 request with group ID equal to destination key and dedupe ID equal to the claimed dispatch ID.
- Given an uncertain SQS result or failed post-send state update, redispatch reuses the claimed generation and dispatch ID.
- Given a future retry, the next due dispatch increments the generation and uses a new dispatch ID, including when it occurs inside SQS's deduplication window.
- Given SQS acceptance followed by a failed state update, redispatch remains safe and worker state suppresses an extra known post.
- Given an unknown SQS send outcome, the record remains recoverable and an alarmable metric is emitted.
- Given two destinations, one destination's ready-work ordering and rate delay do not block the other FIFO group.
- Given queue receives that perform no Slack call, the network-attempt counter does not change.
- Given a FIFO batch failure, the worker stops the batch and returns the failed and all unprocessed records in `batchItemFailures`.
- Given a retry scheduled through `next_action_at`, the worker acknowledges the current queue message and the dispatcher enqueues a fresh request only when due.
- Given a scheduled retry and newer ready work for one destination, the newer work may post first; both still obey destination pacing.
- Given redrive, the request reaches a FIFO DLQ with enough evidence to locate its delivery record.

## Slack acceptance

- Given valid incoming-webhook or bot configuration, the deployed worker posts a synthetic message to each route and records `posted`.
- Given routes sharing a destination, configuration validation rejects duplicate destination aliases or makes them use one identical destination key and pacing record, according to the concrete mode rule.
- Given source text containing Slack control characters or mention syntax, rendering displays it as text and does not create a source-controlled mention.
- Given high priority, only the configured user-group ID may be mentioned.
- Given `429`, the worker bounds `Retry-After`, releases work to durable scheduling, and performs no Lambda sleep for the delay.
- Given a reviewed retryable server response, attempts stop at `max_network_attempts` and terminal escalation is visible.
- Given a permanent credential, channel, hook, or payload error, state becomes `failed_terminal`.
- Given a timeout after bytes may have been sent or a worker crash during `sending`, state becomes `delivery_unknown` and is not retried automatically.
- Given duplicate queue delivery after `posted`, the worker performs no Slack call.

## Recovery acceptance

- Given overdue pending work, the reconciler makes it dispatchable or raises a bounded-age alarm.
- Given an expired sending lease, the reconciler marks it unknown.
- Given a missing queue message and eligible known-safe state, the reconciler re-enqueues it.
- Given a DLQ item, the runbook can trace request, candidate, release, destination, and last state.
- Given an operator-approved unknown replay, audit history records operator, reason, old attempt, and new attempt before another call.

## Security acceptance

- Feed and dispatcher roles cannot read Slack secrets.
- Slack worker can read only configured Slack secret resources.
- No runtime role can assume a customer-account role.
- Bucket, queue, table, key, log, and secret policies pass least-privilege review.
- Feed and Slack URL validation has DNS-rebinding and redirect regression tests.
- Logs and fixtures contain no credential values or complete webhook URLs.
- Route-isolation integration tests prove that customer context cannot cross a destination boundary.

## Operations and capacity acceptance

- Every component emits a heartbeat and its documented metrics.
- Feed freshness is measured per feed and includes newest observed publication age.
- Alarm subscriptions are configured by automation and receipt of a test notification is confirmed by an operator.
- A load test at the declared envelope records throughput, duration, concurrency, DynamoDB throttling, queue age, destination pacing, and Slack response classes.
- Queue visibility, receive threshold, retry delay, network attempts, and reserved concurrency satisfy the tested timing model.
- Shadow mode evaluates live snapshots without candidate or delivery writes.
- Release rollback and application rollback exercises preserve historical replay.

## Implementation order

1. Keep schemas, examples, semantic validation, identity test vectors, and ADRs green.
2. Build the historical corpus and matcher evaluation harness.
3. Build immutable release publishing, promotion, loading, and rollback.
4. Implement safe feed fetching, snapshots, source state, normalization, and coalescing.
5. Implement matching, profile mapping, candidate construction, and durable outbox writes.
6. Implement dispatcher, FIFO queue handling, Slack worker, delivery states, and reconciler.
7. Add Terraform bootstrap and central roots with IAM, encryption, schedules, indexes, logs, alarms, and packaging.
8. Run integration, recovery, load, security, shadow, and production preflight tests.

Each step ends with executable evidence before the next layer treats it as a dependency.

## Required test fixtures

- Valid RSS and Atom documents from each supported AWS feed shape.
- Conditional `304`, duplicate item, overlapping feeds, edited item, missing date, malformed XML, entity expansion, unsafe URL, redirect, oversized response, and partial feed failure.
- Service/risk positives, hard negatives, phrase boundaries, Unicode, HTML, distinct evidence spans, and excluded terms.
- One route, shared destination, separate destinations, disabled environment, changed profile, and route isolation.
- Every delivery transition, queue duplicate, state-write failure after SQS acceptance, `429`, bounded retry, terminal error, timeout, worker crash, stale lease, DLQ, and manual replay.
- Release hash mismatch, incompatible version, concurrent promotion, retained rollback, and expired terminal replacement.

## Generation rules

Generated code and infrastructure must follow the current contracts and accepted ADRs. Do not generate placeholder external interfaces, customer roles, account data collectors, or unbounded plugin hooks. Keep producer, matcher, release, state, dispatch, Slack, and operations packages separate behind typed internal interfaces. Runtime names and IAM policies derive from validated deployment input.

## References

References verified: 2026-07-13.

- [AWS Lambda testing guide](https://docs.aws.amazon.com/lambda/latest/dg/testing-guide.html)
- [AWS Well-Architected operational excellence](https://docs.aws.amazon.com/wellarchitected/latest/operational-excellence-pillar/welcome.html)
- [AWS Well-Architected security](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/welcome.html)
