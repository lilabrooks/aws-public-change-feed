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

## Lambda role and lifecycle audit at runtime cutover

Before trusting the runtime deployment, repeat the two checks that found every defect in the Terraform data-plane audit. Each defect was valid HCL that `terraform validate` and review both passed, planned and applied cleanly, and then matched nothing at runtime.

1. Simulate every role against every resource it must reach, DynamoDB global secondary indexes included. A GSI is a distinct IAM resource, so a policy naming only the table ARN implicitly denies `dynamodb:Query` on `status-next-action-index` and `dynamodb:GetItem` on the delivery table that `outbox.emit` calls before every write. Use `aws iam simulate-principal-policy --policy-source-arn <role> --action-names <actions> --resource-arns <table, index, queue, topic, bucket arns>`.
2. Read the applied bucket lifecycle, not the source: `aws s3api get-bucket-lifecycle-configuration`. On a versioned bucket, expiration writes a delete marker and leaves the body noncurrent, which expires only when both `NoncurrentDays` and `NewerNoncurrentVersions` are exceeded, so a keep-N rule can never reach a lone version. Filters must name the exact key or prefix, not a broader tree.
3. Make a check actually fail before recording it as passing. Simulation proves the policy allows a call, not that the runtime issues it; an allowed call the code never makes is still a first-invocation failure.

## Feed stale or fetch failing

1. Identify the feed and compare first attempt, last attempt, last success, newest observed publication time, ETag, Last-Modified, state version, lease owner and expiry, and error class. Before the first success, freshness age starts at the durable first attempt and must not reset on each failure.
2. Check the scheduler and watcher heartbeat. If all feeds are stale, inspect the shared runtime, DNS, egress, release load, and source-table errors first.
3. Classify failed watcher invocations from the function-scoped custom metrics.
   `WatcherFaults` means an unexpected internal fault and pages on one
   occurrence. `IncompleteRuns` means the remaining-time reserve or a bounded
   conditional-state retry stopped the pass; it pages only after two
   consecutive 15-minute periods. The AWS/Lambda `Errors` alarm is a matching
   two-period backstop only while the watcher runtime is enabled.
   `ReleaseVerificationFailures` is diagnostic and has no notification
   actions; `WatcherFaults` owns paging for that invocation. EventBridge
   retries can contribute samples, so two breaching periods do not prove that
   two distinct scheduled events failed.
4. A faulting invocation may stop before it emits the dimensionless
   `MaxFeedStalenessSeconds` aggregate. Its absence is unknown, not evidence
   that feeds are fresh. Use the invocation error and custom metric, then check
   durable first and last attempt, last success, pending validators, state
   version, lease owner and expiry, and error class for each configured feed.
5. For one feed, verify the configured URL and approved host against the publisher's official feed page.
6. Inspect safe diagnostics for DNS classification, TLS, content type, redirect rejection, response size, timeout, parser limit, and raw-snapshot status.
7. Do not relax the host allowlist, address checks, TLS, redirect, or parser limits during incident response. Add a reviewed configuration or code change with a regression fixture.
8. Run a shadow fetch using the deployed network path. It must not update validators or create candidates.
9. Restore service, confirm last success advances, and inspect the next normalized item count. Keep the prior ETag and Last-Modified until processing is durable.

If an approved feed URL changes intentionally, configure it under a new feed
name. Preserve the old feed's state until a separate retirement review; do not
reuse its validators for the new location.

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

1. Find the feed response-run ID, the page-set ID derived from its current sorted candidate IDs, and that set's deterministic pages. Confirm the zero-candidate case has one empty page and every other page contains at most 25 sorted IDs. More than one page set under a response run can be valid when cross-feed coalescing changed between attempts.
2. Confirm each page marker, candidate, delivery item, and announcement emission reference survives durable read-back, then check whether the feed validator advanced.
3. If the validator did not advance, rerun normal feed processing. Conditional writes should fill missing records.
4. If the validator advanced without every required outbox record, treat it as a correctness incident. Pause the watcher, preserve state and snapshots, and repair through a reviewed recovery tool.
5. Do not synthesize a new candidate ID. Recompute from the exact source revision and release.
6. After repair, verify final response-run read-back and the batch checkpoint transaction. Resume only after confirming the exact lease owner and state version; never overwrite a newer owner.

## Outbox or queue backlog

1. Compare oldest `pending_queue`, `queued`, and `failed_retryable` ages with SQS visible, in-flight, and oldest-message metrics.
2. Check dispatcher and reconciler heartbeat, delivery-table throttles, index status, worker errors, reserved concurrency, and destination pacing.
3. If DynamoDB has pending work and SQS is empty, inspect dispatcher sends and state-update conditions.
4. If SQS has work and the worker is idle, inspect the event source mapping, permissions, concurrency, and visibility timeout.
5. If one destination is delayed, inspect its pacing record and recent `Retry-After`; keep other FIFO groups running.
6. Scale only within the validated support envelope. Slack destination pacing may remain the limiting factor after Lambda capacity increases.
7. Confirm backlog age falls and no new unknown outcomes appear.

The reconciler runs every five minutes and may repair at most 100 records. A
`StateObservationSaturated` or `RecoveryRepairLimitReached` alarm means the run
was deliberately incomplete and failed so EventBridge could retry it. Confirm
that repeated runs reduce the oldest durable age. Do not raise the limits until
the accepted capacity reopening conditions have been reviewed.

An old `queued` record is a signal only. Do not enqueue it manually from age
alone: SQS has no authoritative lookup proving that its recorded message is
absent. Inspect queue depth, in-flight messages, worker concurrency, event-source
mapping health, and the durable message ID first.

## Slack retryable or terminal failure

1. Locate the delivery record by request or candidate ID and inspect response class, network-attempt count, next action, and destination key.
2. For rate limiting, validate the bounded `Retry-After` and destination pacing record. Do not force immediate retry.
3. For server errors, allow the reviewed backoff policy to continue until its network-attempt limit.
4. For a terminal credential, hook, workspace, channel, or payload error, disable only the affected route if messages would otherwise accumulate without value.
5. Classify the record under [ADR-021](../adr/021-audited-terminal-record-replay.md).
   Exact-request replay accepts only its enumerated credential, hook,
   authorization, membership, channel, archive, and exhausted-budget outcomes.
   Refuse immutable candidate/release/application disagreement, stored
   destination or payload defects, renderer failures, `http_400`, malformed or
   unknown outcome metadata, every unlisted response class, and any class that
   cannot truthfully carry `attempts_exhausted = true`.
6. Correct the mutable secret or Slack-side condition through its normal
   reviewed path. Run the synthetic destination preflight against the exact
   stored route and request conditions.
7. Record the current state version. Preview with `python3
   scripts/replay_delivery.py --table-name <table> --candidate-id <candidate>
   --expected-state-version <version> --operator <operator> --reason <reason>
   --evidence <preflight-evidence> --terminal-replay`. Confirm the response
   class, exhausted-budget flag, prior attempt, and live terminal expiry. The
   preview performs no write.
8. Repeat with `--apply`. A successful result reports `TerminalReplay: 1`,
   appends dedicated bounded history, removes the terminal TTL, preserves the
   historical network-attempt count and exact request/release/application
   identity, and reserves one attempt. A refusal means the state, version,
   attempt, expiry, response, reservation, or history capacity changed.
9. Treat `read_failed` as a proved pre-write failure. Treat `ambiguous` as an
   unknown write result and reread before any retry. `applied_after_reread`
   means the command proved its exact history entry and reservation after the
   write response was lost.
10. Leave an expired terminal record unchanged. A correction requiring a newer
    release or application belongs to the separately tracked reissue decision;
    do not rewrite the historical candidate or request.

## Delivery unknown

Automatic retry is forbidden because Slack may already contain the message.

1. Record the candidate ID, request ID, attempt ID, destination, request start time, lease expiry, and worker invocation.
2. Search the destination around that time using title, source link, and compact candidate ID. Respect customer access boundaries.
3. If the message exists, record the delivery record's current state version.
   Preview the reconciliation with `python3 scripts/replay_delivery.py
   --table-name <table> --candidate-id <candidate> --expected-state-version
   <version> --operator <operator> --reason <reason> --evidence <evidence>
   --found-post --terminal-retention-seconds <retention>`, adding
   `--slack-message-ts`, `--slack-permalink`, or `--slack-reference` only for
   bounded identifiers. Repeat with `--apply` to conditionally mark `posted`.
   A successful result reports `FoundPostReconciliation: 1` and creates no new
   attempt.
4. If the message is absent and the operator accepts the remaining duplication
   risk, record the delivery record's current state version. Preview the
   mutation with `python3 scripts/replay_delivery.py --table-name <table>
   --candidate-id <candidate> --expected-state-version <version> --operator
   <operator> --reason <reason> --evidence <evidence>`. The preview performs a
   strongly consistent read and no write. Review its redacted hashes against
   the supplied operator, reason, and evidence.
5. Repeat the same command with `--apply` to make one conditional mutation. A
   successful result reports `ManualReplay: 1`, the new state version, and the
   attempt ID reserved before the next Slack call. A refusal means the record
   changed; inspect the fresh state. A `read_failed` result proves the initial
   read failed before any replay write was attempted; restore read access and
   repeat the command. An ambiguous AWS result requires a direct strongly
   consistent reread before any retry.
6. If evidence is inconclusive, leave the state unknown and escalate to the service owner.
7. Review timeout, lease, visibility, and worker termination evidence before closing the incident.

## DLQ response

1. Stop bulk redrive. Inspect a sample and map each request ID to its delivery
   record and exact release. Separate poison payloads, incompatible contracts,
   permission failures, pacing redeliveries, worker faults, and state-transition
   conflicts. Fix and test the cause with a regression fixture.
2. Confirm the exact source FIFO URL and delivery-DLQ URL. Preview a movement
   at one message per second:

   ```bash
   python3 scripts/redrive_delivery_dlq.py preview \
     --source-queue-url <delivery-fifo-url> \
     --dlq-url <delivery-dlq-url> \
     --max-messages-per-second 1
   ```

   The preview verifies both FIFO attributes, the exact source redrive policy,
   the DLQ's exact `byQueue` allow policy, approximate visible depth, and recent
   movement tasks. It receives no message and starts no task.
3. Review the preview and the deployed package required by the queued records.
   Start the task by changing `preview` to `start` and adding `--apply`. Omit a
   custom destination; the command returns work to the configured source queue.
4. Inspect progress with the same queue arguments and the `status` action. If
   the first records do not produce the expected durable outcomes, cancel the
   exact running task with `cancel --task-handle <handle> --apply`. An ambiguous
   start or cancellation result is followed by `status` before any retry.
5. Confirm no duplicate network call occurs for `posted` or active `sending`
   records. Expand velocity gradually while watching source-queue age, DLQ
   depth, unknown outcomes, and terminal failures.

Native SQS redrive is asynchronous and does not impose an exact message-count
boundary. Queue and task counts are approximate, cancellation can lag, and
messages entering the DLQ during a running task can join it. Redriven work can
interleave with new source traffic; SQS assigns it new message IDs and enqueue
times. Revisit an exact finite-message redrive cap or a custom per-message
mechanism if controlled L-09 deployment evidence shows that velocity and
cancellation do not give operators enough control.

## Scheduled runtime failure

The standard runtime-failure queue contains exhausted EventBridge target events,
not delivery requests. Never redrive it into the delivery FIFO queue.

1. Identify the exact schedule rule, target Lambda, event ID, and event time.
2. For a watcher event, use `WatcherFaults`, `IncompleteRuns`, AWS/Lambda
   `Errors`, durable feed state, and snapshot evidence. For a dispatcher event,
   use its function-scoped heartbeat, Lambda error, dimensionless dispatch
   counts, delivery-record claim, DynamoDB throttling, and exact SQS send
   evidence. For a reconciler event, use the reconciler Lambda error,
   `ReconcilerFault`, observation saturation, repair limit, DynamoDB throttling,
   and SQS send evidence for that invocation.
3. Confirm whether later scheduled runs completed the durable work. The watcher
   runs every 15 minutes, the dispatcher every minute, and the reconciler every
   five minutes. Conditional operations make a repeated schedule event safe,
   but a failed invocation may already have made part of its bounded durable
   progress.
4. Fix the source-level or permission failure and invoke one reviewed schedule
   event against the same deployed package before deleting the failure message.
5. Preserve messages whose durable outcome is still unexplained.

## Release failure or rollback

1. Identify the active manifest S3 version and the last known good immutable release.
2. Verify object keys, object version IDs, hashes, schema versions, and application compatibility.
3. Read the failure code before acting, because they mean different things. `412` means another publisher promoted first: stop, and record both release IDs rather than retrying. `409` leaves the outcome unknown: re-read the pointer and treat the release it names as the current state. `404` means the pointer is missing or carries a delete marker: stop and escalate, and never re-create it by switching to a create precondition. A 403 on the preceding current-pointer read triggers one exact-key, one-result list probe. No exact key enters the first-promotion decision. An exact key, a failed probe, or a malformed response remains a refusal and requires a permission investigation.
4. Promote the retained prior pointer version through the normal conditional process, writing it forward as a new document with a fresh `promoted_at` rather than republishing the historical bytes.
5. Run a read-only load and shadow match probe.
6. Confirm watcher, dispatcher, and worker can still load historical releases referenced by in-flight delivery records.
7. Record the failed and restored release IDs.

## Configuration release publication

Use the reviewed Terraform backend principal to initialize the remote backend
and capture its applied outputs. Then use the release-publisher role for the
reviewed deployment during preview and apply. The configuration file is a
separately reviewed version-4 policy whose environment policies must cover the
deployment environments exactly. Keep the generated inventory, canonical
plan, plan digest, and bounded command output with the change record.

1. From a clean checkout, capture the applied central outputs without editing
   them:

   ```bash
   mkdir -p build
   terraform -chdir=infra/central init -input=false
   terraform -chdir=infra/central output -json > build/central-outputs.json
   ```

   `tests/fixtures/terraform-output.dev.json` is non-secret test data for the
   local loader proof. It cannot replace a fresh output capture for preview or
   apply.

2. Record the exact published application package digest. Choose explicit
   second-precision UTC values for inventory generation and pointer promotion;
   the promotion value must follow the pointer version the preview reads.
3. Generate and validate the inventory, read the current pointer, and write one
   canonical plan:

   ```bash
   python3 scripts/publish_release.py preview \
     --deployment infra/central/deployment.yaml \
     --config config/dev.yaml \
     --terraform-output build/central-outputs.json \
     --inventory build/inventory.json \
     --plan build/config-release-plan.json \
     --application-version sha256:<64-lowercase-hex> \
     --generated-at <YYYY-MM-DDTHH:MM:SSZ> \
     --promoted-at <YYYY-MM-DDTHH:MM:SSZ>
   ```

   Preview performs no S3 write. It refuses a malformed or mismatched Terraform
   output, validates the deployment, configuration, and generated inventory as
   one bundle, and reports the release ID, object keys, pointer identity, and
   plan SHA-256 without printing the inventory or a provider exception.
4. Review the exact input hashes, generated inventory hash, selected package
   digest, current pointer ETag and version, release ID, object keys, and times
   in `build/config-release-plan.json`. Preserve the reported plan SHA-256.
5. Apply only those plan bytes:

   ```bash
   python3 scripts/publish_release.py apply \
     --plan build/config-release-plan.json \
     --expected-plan-sha256 <preview-plan-sha256>
   ```

   Apply reloads and revalidates every local input, regenerates the inventory,
   and repeats the pointer read before the first S3 write. Any difference is
   `stale_plan`; create a fresh preview instead of editing the plan.
6. Treat only `status=completed` as completion. A completed result records
   `promotion.status=promoted` or `promotion.status=converged` and the exact
   release returned by the compatibility probe. Preserve release-object version
   IDs and prior/new pointer identities.
7. On `promotion_superseded`, record both release IDs and start again from a
   fresh preview. On `promotion_conflict`, the 409 re-read named another release.
   On `pointer_vanished`, stop and escalate. On `probe_failed`, preserve the
   pointer result and release-object versions, then repair compatibility before
   declaring the release usable. These results can leave matching immutable
   release objects in place; a later fresh plan adopts their exact versions.

The command defines the operator path and has injected-store plus moto coverage.
Running it against the dev bucket is a live AWS operation and requires separate
deployment authority. Its local tests do not prove an applied release, Lambda
execution, public-feed processing, or Slack delivery.

## Application package rollout and rollback

1. Pause candidate creation at its event source. Record the current Lambda package digest and query actionable delivery records for every distinct embedded `application_version`.
2. Drain work for the current digest within the approved rollout window. Record every version that remains in `pending_queue`, `queued`, `sending`, or `failed_retryable`; leave `delivery_unknown` as evidence.
3. Run `python3 scripts/build_lambda_package.py --output build/slack-worker.zip`. Keep the reported `sha256:<digest>` with the change record. Build twice with the same Python and pip toolchain when runtime source, production dependencies or their lock, packaged schemas or assets, or package-builder inputs changed. Compare the exact bytes or SHA-256 digests. Documentation, site, test-only, and Terraform-only changes do not trigger this double build by themselves. A mismatch is a packaging change and must be reviewed as such.
4. Publish with `python3 scripts/publish_lambda_artifact.py --bucket <config-bucket> --prefix <top-prefix>/application-artifacts --package build/slack-worker.zip`. Publication uses `If-None-Match: *`; an existing matching digest is adopted, and existing bytes are never replaced.
5. Apply `infra/central` with the worker, watcher, and dispatcher digest and VersionId pairs from the publisher and `delivery_triggers_enabled=false`. Terraform refuses the watcher or dispatcher unless each pair exactly equals the worker pair, derives one digest key, deploys that exact S3 version to all three functions, and injects `APPLICATION_VERSION=sha256:<digest>` where the runtime consumes it. This apply creates the watcher and dispatcher rules and the worker event-source mapping in a disabled state.
6. Read all three Lambda configurations before activation. Confirm their identical digest and code S3 version; the watcher's 300-second timeout, concurrency one, 360-second lease, and disabled 15-minute rule; the dispatcher's 60-second timeout, concurrency one, disabled one-minute rule, two retries, 300-second event age, and exact failure-queue source policy; and the worker's 300-second timeout, disabled FIFO event-source mapping, batch size 10, `ReportBatchItemFailures`, and 1,800-second queue visibility. Create the exact disabled-runtime plan, record the printed digest, review its bounded identities, and then apply only those plan bytes:

   ```bash
   python3 scripts/preflight_delivery.py preview \
     --deployment infra/central/deployment.yaml \
     --config config/dev.yaml \
     --terraform-output build/central-outputs.json \
     --expected-account <12-digit-account> \
     --application-digest <64-hex-package-digest> \
     --candidate-cap 10 \
     --plan build/delivery-preflight-plan.json

   python3 scripts/preflight_delivery.py apply \
     --plan build/delivery-preflight-plan.json \
     --expected-plan-sha256 <printed-plan-sha256>
   ```

   Preview reads credential metadata only and invokes no runtime. Apply repeats every preview check, invokes the watcher once, refuses dispatch when the real public-feed cohort exceeds the cap, dispatches a bounded cohort once, and sends one real dispatcher-produced request through the deployed worker. It deletes that exact SQS receipt only after the durable record reports `posted`; other cohort messages remain behind the disabled mapping. `no_positive_match` ends the attempt without extending the sample. `candidate_cap_exceeded`, an unknown invocation or deletion result, a stale plan, a changed identity, or any refused state is not success and must not be retried from the old plan. Preserve the bounded output, confirm the named Slack channel received the `posted` candidate, and record the candidate and request IDs before step 7. While `delivery_triggers_enabled=false`, these Terraform alarm resources are absent: `aws_cloudwatch_metric_alarm.feed_watcher_errors` (`apcf-<deployment>-feed-watcher-errors`), `aws_cloudwatch_metric_alarm.watcher_incomplete_runs` (`apcf-<deployment>-watcher-incomplete-runs`), `aws_cloudwatch_metric_alarm.watcher_fault` (`apcf-<deployment>-watcher-fault`), `aws_cloudwatch_metric_alarm.feed_watcher_heartbeat` (`apcf-<deployment>-feed-watcher-heartbeat`), `aws_cloudwatch_metric_alarm.dispatcher_errors` (`apcf-<deployment>-outbox-dispatcher-errors`), and `aws_cloudwatch_metric_alarm.dispatcher_heartbeat` (`apcf-<deployment>-outbox-dispatcher-heartbeat`). Use the parenthesized CloudWatch name patterns for console or CLI searches. Inspect the preflight evidence and function logs directly; paging from those alarms begins after step 7 enables the delivery cohort.
7. Review a second plan that changes `delivery_triggers_enabled` to `true`. Apply only those unchanged plan bytes, then read back both rules, the worker mapping, and the runtime-only alarm set. Resume candidate creation after all three composition roots use the same digest and exact S3 version and every readback reports enabled.
8. For rollback, pause candidate creation, select the retained digest required by queued work, locate its digest key, exact S3 version, and reviewed deployment input from that rollout, then deploy them together with `delivery_triggers_enabled=false`. The credential store and delivery mode are composition inputs and must still agree with the candidate's exact inventory release. Repeat configuration and preflight checks before enabling the delivery cohort or redriving a small batch.
9. If a delivery references a package absent from the artifact prefix, leave its record unchanged and record the bounded reason `artifact_unavailable`. Restore the approved package or make a documented manual closure; current code cannot reinterpret the candidate under another digest.
10. Finish or abandon every publication, rollout, rollback, and restoration before retirement. Assume the separate `application_artifact_retirement` role; never add deletion to the publisher or a runtime role.
11. Record every protected `digest:VersionId` pair required by active deployment, rollout, or rollback. If there are none, record that assertion explicitly.
12. Preview with `python3 scripts/retire_lambda_artifacts.py preview --deployment <reviewed-deployment.yaml> --schema schemas/deployment.schema.json --plan <plan.json> --inventory-limit <positive-bound> --protected <digest:VersionId>`. Use `--no-protected-packages` only when the reviewed protection set is empty. Review every retained row and deletion candidate, then record the reported plan SHA-256. Preview does not delete.
13. Apply the unchanged plan with the same deployment, protection arguments, and inventory limit, plus `apply --expected-plan-sha256 <sha256>`. A stale plan, refused conditional delete, failed delete, ambiguous exact-version read, partial run, or failed final inventory exits nonzero. Do not retry an old plan after any proved deletion; inventory it again and create a fresh preview.
14. Treat only `applied` as completion. Preserve the plan, output, exact role identity, and proved-deleted/untouched lists. No package retirement is part of repository verification or deployment.
15. To restore, obtain the approved exact ZIP bytes, recompute their digest, publish through `publish_lambda_artifact.py`, and record the new VersionId before rollout or rollback. Permanent deletion cannot restore the old VersionId.

The recovery reconciler uses the same built package bytes but separate Terraform
inputs: `reconciler_artifact_sha256` and `reconciler_artifact_version_id`. Setting
them with `reconciler_trigger_enabled=false` must not change the worker inputs
or the delivery trigger gate. Confirm the 60-second timeout, reserved concurrency
one, disabled five-minute rule, 100/101 repair and observation bounds, two
retries, 300-second event age, and exact runtime-failure queue policy. After its
separate preflight, review and apply a plan that changes only
`reconciler_trigger_enabled` to `true`, then read the rule and heartbeat alarm
back before recording the recovery runtime as enabled.

## Isolated recovery and load exercise

This procedure implements ADR-024. Every AWS command in it needs separate
owner authorization for its exact create, exercise, trigger, or teardown
mutation. Keep the private Slack webhook and operational email endpoint out of
Git and shell history.

1. Initialize `infra/preflight` with the reviewed backend principal. Confirm
   account `667653114001`, region `us-east-1`, state key
   `apcf/preflight/terraform.tfstate`, deployment ID `preflight`, and config
   bucket `apcf-config-preflight-667653114001` before planning.
2. Apply an unchanged saved plan with all artifact inputs null and every
   trigger disabled. Capture `terraform output -json` after apply. This creates
   only isolated mutable resources and the private credential container.
3. Publish the exact dev package bytes to the preflight config bucket at
   `apcf/application-artifacts/<digest>.zip`. Verify its digest metadata and
   content length and bytes against the persistent object. This copy is the
   worker's isolated availability catalog. Record, but do not use, its new
   VersionId as the Lambda code source.
4. Publish and promote a release into the preflight config bucket using
   `infra/preflight/deployment.yaml`, `config/dev.yaml`, and freshly captured
   preflight outputs. Supply the private Slack webhook only to the
   `preflight/slack/private-test-webhook` secret.
5. Create and review a saved Terraform plan that supplies the persistent dev
   package digest and its exact persistent VersionId while
   `exercise_load_triggers_enabled=false`. Apply those unchanged bytes and
   capture fresh outputs.
6. Preview the recovery protocol without invoking a runtime:

   ```bash
   python3 scripts/preflight_runtime_exercise.py preview \
     --protocol recovery \
     --terraform-output build/preflight-recovery-outputs.json \
     --terraform-plan build/preflight-recovery.tfplan \
     --expected-account 667653114001 \
     --application-digest <64-lowercase-hex> \
     --application-version-id <persistent-dev-version-id> \
     --plan build/preflight-recovery-plan.json
   ```

   Review the account, state key, artifact source and mirror, active release,
   private destination, function identities, empty queues, disabled triggers,
   two-record bound, persistent Terraform/table/queue/alarm configuration
   baseline, IAM denials, and both plan hashes. Then apply only that canonical
   plan:

   ```bash
   python3 scripts/preflight_runtime_exercise.py apply \
     --plan build/preflight-recovery-plan.json \
     --expected-plan-sha256 <printed-plan-sha256> \
     --evidence build/preflight-recovery-evidence.json
   ```

   `passed` requires the due pending record to reach `posted` with one network
   attempt and the expired sending record to reach `delivery_unknown` with zero
   attempts. Preserve any other result without rerunning the old plan.
7. Create and review a Terraform plan that changes only
   `exercise_load_triggers_enabled=true`. Apply those exact bytes, verify the
   watcher and reconciler remain disabled while dispatcher and worker report
   enabled, and capture fresh outputs. Run `preview --protocol load` with that
   output and saved enablement plan, then run `apply` with the printed digest.
   The runner creates 5 records per minute for exactly 10 minutes, stops at 50,
   observes a bounded five-minute drain, and records durable states, Slack
   response classes and latency, end-to-end duration, network-attempt counts,
   destination pacing, outbox age, both DLQs, delivery-unknown counts, Lambda
   invocation, duration, concurrency, error and throttle metrics, DynamoDB
   throttles, SQS age, active dispatcher and worker log streams, alarm
   transitions, and the exact candidate IDs. Missing primary evidence makes
   the result `incomplete`; a quiet run is not extended. The final evidence
   also requires the persistent control baseline to remain exact and confirms
   that none of the 50 synthetic candidate IDs exists in the persistent dev
   delivery store. Log presence comes from a bounded `FilterLogEvents` search
   over the exact exercise window. The collector follows at most ten pages,
   retains one stream name with event and ingestion timestamps per runtime,
   and discards message bodies. Do not use `DescribeLogStreams.lastEventTimestamp`
   for this check; AWS documents that field as eventually consistent and says
   it can lag ingestion by an hour or longer.
8. Apply an unchanged plan returning
   `exercise_load_triggers_enabled=false`. Read back all four disabled trigger
   states before cleanup.
9. Create a saved Terraform destroy plan and its JSON form:

   ```bash
   terraform -chdir=infra/preflight plan -destroy -out=../../build/preflight-destroy.tfplan
   terraform -chdir=infra/preflight show -json ../../build/preflight-destroy.tfplan > build/preflight-destroy.json
   ```

   Preview the exact cleanup inventory:

   ```bash
   python3 scripts/preflight_runtime_exercise.py teardown-preview \
     --terraform-plan build/preflight-destroy.tfplan \
     --terraform-plan-json build/preflight-destroy.json \
     --expected-account 667653114001 \
     --config-bucket apcf-config-preflight-667653114001 \
     --plan build/preflight-teardown-plan.json
   ```

   Apply only after reviewing every address and the plan digest:

   ```bash
   python3 scripts/preflight_runtime_exercise.py teardown-apply \
     --plan build/preflight-teardown-plan.json \
     --expected-plan-sha256 <printed-plan-sha256> \
     --evidence build/preflight-teardown-evidence.json
   ```

   The command deletes only exact versions and delete markers in the named
   preflight bucket, applies the unchanged destroy plan, and requires an empty
   preflight Terraform state. If cleanup fails, leave every trigger disabled
   and preserve the exact remaining inventory. Never widen deletion to the
   state bucket or persistent artifact object.

## Manual source replay

1. Name the retained raw snapshot or bounded time range, target release, purpose, operator, and expected route scope.
2. Run dry mode first and compare candidate IDs with existing candidate history.
3. By default, replay fills missing state and suppresses existing candidates.
4. Any request to resend an existing candidate uses the manual delivery replay path and its audit fields.
5. Keep feed validators unchanged during snapshot replay.

## Alarm delivery failure

1. Verify that each Git-reviewed deployment descriptor contains only its stable alias and reviewed protocol, currently `email`.
2. Supply endpoints outside Git through the sensitive `operational_sns_subscription_endpoints` map. Its keys must equal the reviewed aliases exactly; remember that the endpoint is then present in encrypted Terraform state.
3. Before the first apply that would own an already confirmed subscription, import that exact subscription at its reviewed address, such as `aws_sns_topic_subscription.operations["primary-email"]`, under separate live authority. Do not create a duplicate subscription.
4. Verify topic policy, subscription state, delivery status logging, and the receiving endpoint or email confirmation.
5. Send the approved synthetic operational notification.
6. Infrastructure checks can prove configuration. An operator records receipt before production readiness is restored.
7. Maintain an alternate escalation path while operational notifications are impaired.

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

References verified: 2026-08-27.

- [AWS IAM policy simulator](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_testing-policies.html)
- [CloudWatch alarm troubleshooting](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/troubleshooting-alarms.html)
- [CloudWatch Logs `DescribeLogStreams`](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_DescribeLogStreams.html)
- [CloudWatch Logs `FilterLogEvents`](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilterLogEvents.html)
- [SQS DLQ redrive](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-configure-dead-letter-queue-redrive.html)
- [Slack incoming webhooks](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/)
- [DynamoDB condition expressions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html)
