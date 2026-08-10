# 5. Security and operations

## Security objectives

- Fetch only reviewed public sources through bounded network behavior.
- Keep Slack credentials within the delivery worker trust boundary.
- Prevent route data from crossing destinations.
- Preserve exact evidence for matching and delivery decisions.
- Limit AWS permissions to named resources and actions.
- Surface stale, terminal, and ambiguous work.

## Data classification

Public announcement content is public but untrusted. Customer labels, account IDs, Regions, routes, profile assignments, and candidate history are internal operational data. Slack URLs and tokens are secrets.

Contracts and fixtures contain no credential values. Logs exclude response bodies, complete Slack payloads, secret identifiers where unnecessary, webhook URLs, tokens, and full internal inventory. Structured logs may include candidate ID, request ID, dispatch ID and generation, feed name, route ID, destination-key hash, state transition, response class, latency, and bounded error codes.

## Network controls

The feed watcher needs outbound TCP 443 to approved public feed hosts. The Slack worker needs outbound TCP 443 to approved Slack hosts. If Lambdas run in a VPC, provide managed egress and required AWS service endpoints without making functions publicly reachable.

Application controls remain mandatory even with network controls:

- URL parsing and hostname allowlists.
- DNS result validation before every connection.
- Rebinding-safe connection behavior.
- TLS hostname and certificate verification.
- Redirect rejection.
- Connection, response, byte, item, and parser limits.

## Encryption

- S3 uses server-side encryption and bucket keys where appropriate.
- DynamoDB uses AWS-owned encryption by default or a customer-managed key selected by deployment policy.
- SQS and its DLQ use SQS-managed encryption by default.
- Secrets use Secrets Manager or SSM SecureString with their configured encryption.
- CloudWatch Logs uses the account baseline or an explicitly managed key.

Customer-managed keys require exact grants for each service and role. Key policies and IAM policies are both validated.

## Stored credential representation

Both `secret_store` values are supported, and the runtime reads the exact
identifier the release names with no prefix, version, or stage selection. An SSM
read always requests decryption, because a ciphertext returned without it would
reach the webhook policy or an authorization header as an opaque wrong value
rather than as a read failure.

An SSM read additionally requires the returned parameter `Type` to be
`SecureString`. Requesting decryption does not establish it: the API ignores
`WithDecryption` for a plaintext `String` or `StringList` and returns the value,
so without checking the type a credential stored unencrypted reads back correctly
and the encryption this chapter requires is silently absent.

The stored content is the whole credential. No structured stored-secret format
exists, so no reader may extract a field from one; surrounding whitespace is
removed and the remainder is opaque.

Two separate checks establish what a credential is, and the first cannot do the
second's job. The configured kind records the delivery mode the reader was built
for, so comparing it to the release detects a mode mismatch between the release
and the deployment's wiring. It is metadata the runtime chose, and it proves
nothing about what an operator stored: a webhook URL pasted into the bot-token
container passes it. A content check on the value itself is what establishes the
stored kind, and each mode has one — the webhook policy for a URL, and the
documented prefix, bounded length, and single-line printable shape for a bot
token.

Credential read failures divide by whether another identical read could succeed.
A permanent failure — an absent identifier, a denied grant, a missing container,
an empty value, a binary secret, a parameter that is not a `SecureString`, or a
successful response whose shape is unusable — is a configuration correction and
resolves delivery terminally. A transient failure — throttling, a service outage,
an internal provider failure, an endpoint connection failure, a read timeout — is
rescheduled with a bounded delay, makes no Slack call, leaves the network-attempt
budget unchanged, and does not advance destination pacing. A provider error code
outside the reviewed permanent set is treated as transient, so an unclassified
failure preserves deliverable work instead of discarding it.

A credential value never appears in a log, a durable record, a fixture, an
exception message, or an object's `repr`. A read failure carries the store, the
condition, and at most a bounded provider error code — a short ASCII alphanumeric
identifier, with anything else replaced by a fixed placeholder — never the value,
the identifier, the provider message, or a response body, and with no provider
exception attached for a chain walker to reach.

## IAM roles

### Release publisher

- Read deployment sources from the delivery pipeline.
- Create objects only under new release prefixes.
- Read back exact versions, which needs `s3:GetObjectVersion`.
- Conditionally update the active manifest key, which needs `s3:GetObject` for the precondition read alongside `s3:PutObject`.
- No Slack credential access.

### Feed watcher

- Read the active manifest and exact release object versions, needing `s3:GetObject` for the pointer and `s3:GetObjectVersion` for the release objects.
- Read and conditionally update source state.
- Put candidate and delivery records and response-run markers.
- Write bounded raw snapshots under the designated prefix.
- No Slack secrets, queue consumption, customer-account calls, or role assumption.

### Outbox dispatcher

- Query the delivery status index.
- Read and conditionally update delivery records.
- Send to the exact FIFO queue.
- No feed writes or Slack secret access.

### Slack worker

- Consume and delete from the exact FIFO queue.
- Read the exact release versions referenced by requests.
- Read only configured Slack secrets or parameters.
- Read and conditionally update delivery and destination records.
- No source-state writes or release publication.

### Recovery reconciler

- Query the delivery status index.
- Read and conditionally update delivery records.
- Send eligible work to the exact FIFO queue when recovery requires it.
- No secret access or external HTTP.

### Terraform and backend

Deployment roles follow least privilege for provisioned resources. Backend access includes exact state and `.tflock` object actions, prefix-conditioned `s3:ListBucket`, and KMS access only when the state bucket uses a customer-managed key.

## Retention

- Raw feed snapshots: 30 days in the canonical deployment.
- Feed and announcement state: 730 days.
- Terminal delivery state: 365 days.
- Retired immutable releases: 400 days and at least 10 releases.
- Logs: 30 days.

Unresolved delivery work cannot expire before resolution. Retention changes must keep releases available longer than any delivery replay or investigation period.

S3 lifecycle enforces the manifest history, the log groups, and the raw-snapshot window. It does not enforce release retirement, and `infra/central` deliberately configures no rule over the release prefix. Release objects are write-once at a per-release key, so they never become noncurrent versions and a noncurrent-version rule cannot reach them. Expiring them by object age instead would delete on age alone, with no notion of which release is active: a deployment that has not republished within the retention window would lose the release its stored candidates still resolve against. Lifecycle also cannot express the “at least 10 releases” floor, which counts releases rather than versions. Retiring superseded releases belongs to the publisher, which knows both the active pointer and the release order. Until that exists, releases accumulate, which preserves the retention invariant above and costs storage.

## Metrics

### Feed acquisition

- Attempts, successes, `304` responses, and failures by feed.
- `FeedStalenessSeconds` (Maximum) with the bounded `FeedName` dimension for time since last success by feed.
- Dimensionless `MaxFeedStalenessSeconds` (Maximum) across the configured feeds for the aggregate alarm.
- Age of newest observed publication by feed.
- DNS, TLS, redirect, response-limit, content-type, and parser rejections.
- Response bytes, item counts, and raw-snapshot failures.

### Matching

- Normalized and coalesced announcements.
- New revisions and provenance-only updates.
- Candidates by service, risk, priority, and route.
- No-match counts and rule-exclusion counts.
- Candidate validation and size failures.

### Delivery

- Outbox records by state and oldest age.
- Dispatch attempts, accepted queue messages, and unknown send results.
- SQS age, receives, redrives, and DLQ depth.
- Slack network attempts, response classes, `Retry-After`, latency, and terminal states.
- Unknown outcomes, manual replays, stale leases, and reconciler repairs.

Use bounded dimensions. Do not use announcement URLs, titles, candidate IDs, customer names, or error messages as metric dimensions.

## Alarms

Production alarms include:

- Feed last success beyond its schedule-based threshold.
- No watcher heartbeat.
- Source-state throttling or errors.
- Oldest `pending_queue`, `queued`, or retryable work beyond the service objective.
- Queue age, worker errors, throttles, and DLQ depth.
- Any `delivery_unknown` or sustained terminal delivery failures.
- No reconciler heartbeat.
- Raw-snapshot or immutable-release verification failures.
- Operational notification delivery test unconfirmed.

Each alarm links to the operations runbook and includes deployment ID, Region, component, and a safe diagnostic query.

## Backup and restore

Enable S3 versioning. Decide whether DynamoDB point-in-time recovery is required before production based on the candidate-history recovery objective. If enabled, test restore into new table names and validate indexes, TTL configuration, and runtime cutover. SQS is transport and is rebuilt from durable outbox state; it is not backed up.

## Operational objectives

The deployment records:

- Maximum feed freshness per source.
- Maximum candidate-to-queue and queue-to-Slack delay under normal load.
- Recovery time for dispatcher, worker, and table failures.
- Acceptable manual-review time for unknown outcomes.
- Corpus precision and recall thresholds.

These are deployment acceptance values, not universal claims in the codebase.

## Release and rollback

Promote policy releases separately from application deployments. Shadow evaluation can compare a candidate release against a historical corpus and live feed snapshot without creating outbox work. Rollback promotes an earlier immutable manifest or application version. Candidate history records which combination produced each result.

Application packages are content-addressed by the SHA-256 digest of the exact
deployable bytes. Retain them for at least 400 days and keep at least the newest
10 versions. Before application rollout, pause candidate creation, drain
actionable work from the current package, record any remaining package versions,
deploy producer and worker roots with one identical digest, and then resume.

The package builder uses a complete exact dependency lock and deterministic ZIP
metadata. Publication conditionally creates
`<top-prefix>/application-artifacts/<sha256>.zip` in the versioned deployment
bucket, reads the returned S3 version back, verifies its bytes against the
digest, and records that version. Terraform deploys that exact key and version
and injects `sha256:<digest>` into the composition root. Publication has no
delete grant. No lifecycle rule covers this prefix; packages accumulate until a
retirement tool can prove both the age and version-count conditions.

Evidence can outlive its runnable package. When that happens, automatic delivery
leaves the record unchanged and reports `artifact_unavailable`; an operator must
restore the approved package or record a manual closure. Package retirement must
prove the age and version-count floors before deletion.

On an exact-version mismatch, the worker checks only the expected digest key's
metadata. A missing key emits the dimensionless `ArtifactUnavailable` metric; a
provider failure emits `ArtifactAvailabilityCheckFailed`. Neither condition
changes the delivery record or permits current code to process it.

## References

References verified: 2026-07-13.

- [IAM best practices](https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html)
- [Lambda security best practices](https://docs.aws.amazon.com/lambda/latest/dg/security-best-practices.html)
- [CloudWatch embedded metric format](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format.html)
- [DynamoDB point-in-time recovery](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/PointInTimeRecovery.html)
- [S3 security best practices](https://docs.aws.amazon.com/AmazonS3/latest/userguide/security-best-practices.html)
