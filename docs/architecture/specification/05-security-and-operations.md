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
- Probe active-manifest absence after a current-read 403, which needs
  `s3:ListBucket` on the configuration bucket with the complete manifest key
  required as `s3:prefix` and `s3:max-keys` bounded to 1.
- For a separate retirement operation, list versions only under the exact
  release prefix and exact active-manifest key, then permanently delete only
  digest-bound exact versions under the release prefix. The manifest key has no
  delete grant.
- No Slack credential access.

### Feed watcher

- Read the active manifest and exact release object versions, needing `s3:GetObject` for the pointer and `s3:GetObjectVersion` for the release objects.
- Use the same exact-key, one-result `s3:ListBucket` probe for an ambiguous
  current-pointer 403.
- Read and conditionally update source state.
- Put candidate and delivery records and response-run markers.
- Write bounded raw snapshots under the designated prefix.
- No Slack secrets, queue consumption, customer-account calls, or role assumption.

### Shadow evaluator

- Uses the feed watcher's exact package digest, S3 VersionId, timeout, normal concurrency, network policy, and active-release inputs. Its reserved concurrency stays at one while an L-42 plan pauses watcher execution at zero.
- Reads the active manifest and exact release object versions, including the same exact-key bounded absence probe.
- Writes only its dedicated CloudWatch log stream and has no event source.
- Receives no DynamoDB, S3 write or delete, queue, secret, customer-account, or role-assumption permission.
- Accepts invocation from a separate operator role scoped to this function. Asynchronous invocation has zero automatic retries, and the L-42 exercise uses a synchronous request.

### Application artifact retirement operator

- Assume a role separate from release publication and every runtime role.
- List object versions only under the exact application-artifact prefix.
- Read metadata and exact versions only in that prefix.
- Permanently delete only an exact version in that prefix.
- Receive no release publication, runtime, secret, queue, table, or customer-account permission.

### Source-state retention migration operator

- Exists only while the one-time ADR-025 migration is under review or running. Its Terraform gate defaults to false.
- Assumes a role separate from every runtime, publisher, and retirement role.
- Scans only the source-state table with `Select=SPECIFIC_ATTRIBUTES` and the exact projected key, item-type, retention, announcement-version, observation-time, and response-page proof attributes.
- Strongly reads and conditionally updates only `ANNOUNCEMENT#*` and `RUN#*` keys in that table.
- Receives no `DeleteItem`, `PutItem`, delivery-table, queue, secret, release-publication, deployment-mutation, or customer-account permission.
- Is removed after the migration result and untouched remainder are recorded. A Terraform and AWS readback proves the role and inline policy are absent.

### Source-state retirement operator

- Exists permanently and remains separate from every runtime, publisher, and
  migration role.
- Assumption requires a `FeedName` session tag. Its DynamoDB leading-key
  condition permits `GetItem` and `UpdateItem` only for
  `FEED#${aws:PrincipalTag/FeedName}` on the source-state table.
- Receives no table scan, query, `PutItem`, `DeleteItem`, delivery-table, queue,
  secret, S3, deployment-mutation, or customer-account permission.
- Uses exact-content preview/apply plans for retirement marking, tombstone
  compaction, and same-URL restoration. Each mutation rereads the active release
  and selected source-state item before writing.

The watcher uses the same content-addressed package digest and exact S3 object
version as the delivery worker. It runs every 15 minutes with a 300-second
timeout, reserved concurrency one, a 360-second feed lease, and a 60-second
remaining-time reserve before another claim. EventBridge retries a failed target
at most twice while the event is no more than 900 seconds old; exhausted events
enter the encrypted runtime-failure queue under a policy scoped to the exact
watcher schedule ARN.

### Outbox dispatcher

- Query the delivery status index.
- Read and conditionally update delivery records.
- Send to the exact FIFO queue.
- No feed writes or Slack secret access.

The scheduled dispatcher uses the same exact content-addressed package digest
and S3 object version as the enabled watcher and worker. It runs every minute
with a 60-second timeout, reserved concurrency one, and the dispatcher's
100-record cap. EventBridge retries a failed target at most twice while the
event is no more than 300 seconds old. Exhausted events enter the encrypted
standard runtime-failure queue under a policy scoped to the exact dispatcher
schedule ARN and account.

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

The scheduled reconciler uses its own exact content-addressed package inputs,
runs every five minutes with reserved concurrency one, and performs no table
scan. EventBridge retries a failed target at most twice while the event is no
more than 300 seconds old. Exhausted events enter an encrypted standard
runtime-failure queue whose policy permits only the exact schedule rule. This
queue is separate from the FIFO delivery DLQ because it carries invocation
evidence rather than delivery requests.

### Terraform and backend

Deployment roles follow least privilege for provisioned resources. Backend access includes exact state and `.tflock` object actions, prefix-conditioned `s3:ListBucket`, and KMS access only when the state bucket uses a customer-managed key.

## Retention

- Raw feed snapshots: 30 days in the canonical deployment.
- Active feed checkpoints: no expiry.
- Announcement history: 730 days from `last_observed_at`.
- Response page-set markers: 730 days from the latest exact observation; the one-time legacy migration starts a fresh 730-day period.
- Removed-feed checkpoints: 730 days from the reviewed retirement decision before permanent tombstone compaction.
- Terminal delivery state: 365 days.
- Retired immutable releases: 400 days and at least 10 releases.
- Logs: 30 days.

Unresolved delivery work cannot expire before resolution. Retention changes must keep releases available longer than any delivery replay or investigation period.

S3 lifecycle enforces the manifest history, the log groups, and the raw-snapshot window. It does not enforce release retirement, and `infra/central` deliberately configures no rule over the release prefix. Release objects are write-once at a per-release key, so they never become noncurrent versions and a noncurrent-version rule cannot reach them. Expiring them by object age instead would delete on age alone, with no notion of which release is active: a deployment that has not republished within the retention window would lose the release its stored candidates still resolve against. Lifecycle also cannot express the “at least 10 releases” floor, which counts releases rather than versions.

`scripts/retire_config_releases.py` owns the publisher-side retirement path. It
protects every release still named by retained manifest history, requires an
explicit operator protection set for candidate, replay, and investigation
evidence, and applies the age and newest-release floors together. Its canonical
plan binds the complete release and manifest inventories. Exact-version reads
settle each delete response, and only a final inventory matching the proved
retained set reports completion.

## Metrics

### Feed acquisition

- Attempts, successes, `304` responses, and failures by feed.
- `FeedStalenessSeconds` (Maximum) with the bounded `FeedName` dimension for time since last success by feed.
- Dimensionless `MaxFeedStalenessSeconds` (Maximum) across the configured feeds for the aggregate alarm.
- Before a feed has any success, staleness is measured from its durable first attempt rather than reset by later failures.
- Age of newest observed publication by feed.
- DNS, TLS, redirect, response-limit, content-type, and parser rejections.
- Response bytes, item counts, and raw-snapshot failures.

### Matching

- Normalized and coalesced announcements.
- New revisions and provenance-only updates.
- Candidates by service, risk, priority, and route.
- No-match counts and rule-exclusion counts.
- Candidate validation and size failures.

The scheduled handler emits `Heartbeat` after validating the event, environment,
and Lambda context and before loading a release. It emits dimensionless
`ReleaseVerificationFailures`, `RawSnapshotFailures`, and
`MaxFeedStalenessSeconds`; per-feed metrics use only the bounded `FeedName`
dimension. A remaining-time stop and exhausted bounded conditional-state retry
provisionally classify the invocation as `IncompleteRuns` with the bounded
`Function` dimension and fail it so the scheduled target can retry. When no
later fault occurs, they do not emit `WatcherFaults`. An unexpected exception
after a provisional incomplete marker replaces it with function-scoped
`WatcherFaults`, so one failed invocation emits exactly one terminal
classification. Every other exception also emits function-scoped
`WatcherFaults` and fails with the same bounded outward error. Every custom
watcher alarm metric has a runtime producer, and the watcher heartbeat alarm
exists only when the watcher runtime is enabled. The AWS/Lambda watcher
`Errors` alarm also exists only when the watcher runtime is enabled.
`ReleaseVerificationFailures` remains a diagnostic metric with no alarm
notification actions; `WatcherFaults` owns paging for the same failing
invocation.

### Delivery

- Outbox records by state and oldest age.
- Dispatch attempts, accepted queue messages, and unknown send results.
- A function-scoped dispatcher heartbeat after strict event and environment validation.
- SQS age, receives, redrives, and DLQ depth.
- Slack network attempts, response classes, `Retry-After`, latency, and terminal states.
- Unknown outcomes, found-post reconciliations, unknown and terminal manual
  replays, stale leases, and reconciler repairs. The operator command reports
  one bounded `FoundPostReconciliation`, `ManualReplay`, or `TerminalReplay`
  count after a successful conditional mutation; durable reconciliation or
  replay history remains the audit authority.
- Native delivery-DLQ task starts, status, approximate moved and remaining
  counts, and cancellation. Provider failure text and message bodies are not
  emitted by the controller.
- Bounded state-observation saturation, repair-limit exhaustion, stale queued
  records, reconciler faults, and scheduled-runtime failure-queue depth.

Use bounded dimensions. Do not use announcement URLs, titles, candidate IDs, customer names, or error messages as metric dimensions.

The separately authenticated operator running delivery-DLQ recovery needs
`sqs:GetQueueAttributes`, `sqs:ListMessageMoveTasks`,
`sqs:StartMessageMoveTask`, and `sqs:CancelMessageMoveTask` on the exact DLQ.
Native movement also requires `sqs:ReceiveMessage` and `sqs:DeleteMessage` on
that DLQ and `sqs:SendMessage` on the exact source queue. These permissions do
not belong to a runtime role. Customer-managed encryption adds the documented
KMS decrypt and data-key permissions; the current queues use SQS-managed
encryption.

## Alarms

Production alarms include:

- Feed last success beyond its schedule-based threshold.
- No watcher heartbeat.
- One unexpected watcher fault, through function-scoped `WatcherFaults`, only
  when the watcher runtime is enabled.
- Watcher incompletion in two consecutive 15-minute CloudWatch periods, through
  function-scoped `IncompleteRuns`, only when the watcher runtime is enabled.
- AWS/Lambda watcher errors in two consecutive 15-minute CloudWatch periods as
  a backstop, only while the watcher runtime is enabled. A period can include
  EventBridge retries and does not prove that a distinct scheduled event failed.
- Source-state throttling or errors.
- Oldest `pending_queue`, `queued`, or retryable work beyond the service objective.
- Queue age, worker errors, throttles, and DLQ depth.
- Any `delivery_unknown` or sustained terminal delivery failures.
- No dispatcher heartbeat and dispatcher AWS/Lambda errors, only when the dispatcher runtime is enabled.
- No reconciler heartbeat, only when the reconciler runtime is enabled.
- Raw-snapshot failures. Immutable-release verification failures retain a
  diagnostic alarm without notification actions because `WatcherFaults` owns
  paging for those invocations.
- Operational notification delivery test unconfirmed.

Each alarm links to the operations runbook and includes deployment ID, Region, component, and a safe diagnostic query.

Operational SNS subscriptions are Terraform-managed. Git-reviewed deployment
input owns only each alias and its allowed protocol; a sensitive Terraform map
supplies endpoints outside Git, and its keys must equal the reviewed aliases
exactly. Endpoints therefore enter encrypted Terraform state. Subscription
confirmation and receipt of a synthetic notification remain operator evidence,
not conclusions Terraform can establish.

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

Promote policy releases separately from application deployments. Shadow evaluation can compare a candidate release against a historical corpus and live feed snapshot without creating outbox work. The source-defined shadow Lambda is invoked directly with the expected release, application digest, and complete sorted feed-name set. It uses the production release loader, pinned feed fetcher, parser, normalizer, matcher, route mapping, candidate builder, and durability orchestration against fresh in-memory stores. Its role can read the active manifest and exact release objects and write its own logs; it has no DynamoDB, S3 write, queue, or secret permission and has no event source. Its bounded result records the invocation ID, feed outcomes, counts, route IDs, and a digest of the sorted candidate IDs rather than source text or candidate payloads. Fixed refusal codes distinguish identity, compatibility, integrity, missing-release, and incomplete outcomes. Rollback promotes an earlier immutable manifest or application version. Candidate history records which combination produced each result.

Application packages are content-addressed by the SHA-256 digest of the exact
deployable bytes. Retain them for at least 400 days and keep at least the newest
10 versions. Before application rollout, pause candidate creation, drain
actionable work from the current package, record any remaining package versions,
deploy the watcher, regular dispatcher, and worker roots with one identical
digest and exact S3 object version, and then resume. The reconciler consumes the
same package bytes through an independent artifact input pair.

Terraform separates deployment from event-source activation. Artifact pairs
create the Lambda functions and their trigger resources. The Boolean
`delivery_triggers_enabled` stays `false` through configuration inspection and
the feed, dispatch, and destination preflights; one later reviewed apply sets it
to `true` for the watcher rule, dispatcher rule, and worker queue mapping as a
single cohort. `reconciler_trigger_enabled` provides the same default-off
boundary for the separately deployed recovery runtime. In this chapter,
"runtime enabled" means its effective trigger state is true. An alarm expressly
limited to an enabled runtime is absent during deployed-disabled staging.
`scripts/preflight_delivery.py preview` binds the reviewed dev deployment,
configuration, applied Terraform outputs, expected AWS account, exact package
digest, active release, disabled rules and queue mapping, Lambda identities and
settings, Slack credential metadata, durable delivery state, and the queue's
reported depth into a canonical plan. Preview never reads the credential value
and never invokes a runtime. `apply` accepts that plan only with its exact
SHA-256 and repeats the preview before making any runtime invocation.

The apply path invokes one watcher pass while the schedules and mapping remain
disabled. Zero matches records `no_positive_match` and ends the attempt without
extending the feed sample. More matches than the reviewed cap records
`candidate_cap_exceeded` and stops before dispatch. A bounded nonzero cohort is
dispatched once; the command receives one real FIFO request from that cohort,
validates it against the active release, directly invokes the deployed worker,
and deletes only that exact receipt after a strongly consistent delivery read
reports `posted`. The remaining real cohort stays queued behind the disabled
mapping. Unknown invocation or deletion outcomes remain non-successful and are
not retried automatically. A `posted` result still requires the operator to
confirm the named Slack destination before the later trigger-enablement plan is
eligible for review.

ADR-024 adds an isolated `infra/preflight` root for the recovery and declared-
envelope exercises. Its state key is `apcf/preflight/terraform.tfstate`; the
bootstrap principal names that exact state object and lockfile alongside the
bootstrap and central keys. The root fixes deployment identity to `preflight`
in the authorized account and region. Its configuration bucket, tables,
queues, functions, logs, alarms, operational topic, secret container, and
private Slack destination are distinct from the persistent dev resources.

The preflight functions load code from the persistent deployment's exact
immutable bucket, digest key, and VersionId. No preflight role may alter or
retire that object. The isolated configuration bucket carries an exact-byte
catalog mirror because the unchanged worker checks artifact availability in
the same bucket as its active release. Preview hashes both exact object
versions and verifies their identities, byte digests, metadata, and lengths
before exercise-state creation.

Individual trigger overrides exist only in `preflight_mode`. Recovery requires
all four triggers disabled and directly invokes the reconciler and worker for
one due `pending_queue` record and one expired `sending` record. Load keeps the
watcher and reconciler disabled, enables only the one-minute dispatcher and
FIFO worker mapping through a reviewed Terraform plan, and creates 5 synthetic
deliveries per minute for exactly 10 minutes. The generator does not extend the
window after a quiet or failed result. Cleanup first binds an exact destroy
inventory and plan digest, removes only exact versions and delete markers from
the preflight configuration bucket, applies the unchanged destroy plan, and
requires an empty preflight state afterward.

The package builder uses a complete exact dependency lock and deterministic ZIP
metadata. Publication conditionally creates
`<top-prefix>/application-artifacts/<sha256>.zip` in the versioned deployment
bucket, reads the returned S3 version back, verifies its bytes against the
digest, and records that version. Terraform deploys that exact key and version
and injects `sha256:<digest>` into the composition root. Publication has no
delete grant. No lifecycle rule covers this prefix. The ADR-022 operator tool
instead binds a complete bounded inventory to a canonical preview plan, proves
the 400-day and newest-10 floors plus exact protected references, and applies
only after the inventory and plan digest still match. Each deletion names the
exact VersionId and recorded ETag, then proves absence with an exact-version
read. Partial, refused, failed, and ambiguous outcomes never report success.

Build the package twice with the same Python and pip toolchain whenever runtime
source, production dependencies or their lock, packaged schemas or assets, or
package-builder inputs change. Compare the exact bytes or SHA-256 digests before
handoff. Documentation, site, test-only, and Terraform-only changes do not
trigger this double build by themselves because they do not enter the archive.

Evidence can outlive its runnable package. When that happens, automatic delivery
leaves the record unchanged and reports `artifact_unavailable`; an operator must
restore the approved package or record a manual closure. Package retirement
proves the age and version-count floors before deletion and keeps rollout,
rollback, publication, and retirement mutually exclusive operator activities.

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
