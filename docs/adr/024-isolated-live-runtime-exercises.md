# ADR-024: Isolated live runtime exercises

- Status: Accepted
- Date: 2026-08-26
- Owner: Lila Brooks
- Relates to: [ADR-007](007-central-slack-delivery-queue-and-worker.md), [ADR-010](010-operations-and-supported-scale.md), [ADR-016](016-production-preflight-and-event-contracts.md)

## Context

The L-09 dev exercise proved that the source-defined triggers were active. It
also proved the deployed recovery reconciler, alarm delivery, EventBridge
retry behavior, and delivery of the queued M1 cohort. The exercised cohort
contains 12 durable Slack posts. Each post completed in one network attempt
with an HTTP 200 response. The measured cohort peaked at roughly 144
deliveries per hour, below ADR-010's declared 300-delivery-per-hour envelope.

Two acceptance cases remain unsafe to manufacture in the persistent dev
deployment. Recovery needs deliberately created `pending_queue` and expired
`sending` records. Load proof needs a fixed cohort at the declared arrival
rate. Creating either cohort in `apcf-delivery-dev` would mix synthetic records
with the delivery system of record. Sending the load cohort through the dev
webhook would also place test traffic in the operational Slack channel.

The central Terraform root is not a reusable test harness today. Its backend
key, deployment checks, configuration resources, and preflight guard target
the persistent dev deployment. Reusing that root in place would risk state or
resource overlap. Local tests and AWS service mocks cover source behavior, but
they do not prove the live Lambda, DynamoDB, SQS, EventBridge, CloudWatch, IAM,
and Slack path together.

## Decision drivers

- Preserve the audit meaning of the persistent dev delivery table and Slack
  channel.
- Exercise the exact application artifact used by the persistent deployment.
- Bound provider writes, message volume, time, and cleanup before a run starts.
- Keep Terraform state, IAM, queues, tables, logs, alarms, configuration, and
  credentials separate from dev.
- Exercise recovery through owned application boundaries rather than direct
  database edits.
- Record partial or failed runs without extending a quiet sample to obtain a
  passing result.
- Make every destructive cleanup step exact, previewed, and reviewable.

## Decision

Add a dedicated `infra/preflight/` Terraform root for live runtime exercises.
It will use its own remote-state key and bootstrap IAM contract. Every mutable
resource will carry a `preflight` deployment identity and will be separate from
the persistent dev resources, including:

- the configuration bucket and immutable release;
- the delivery and source-state tables;
- the delivery queue, delivery DLQ, and runtime-failure queue;
- Lambda functions, schedules, event-source mappings, logs, metrics, alarms,
  and operational topic;
- the Slack credential reference and a private test destination.

The root will reference the exact immutable application object version used by
the persistent dev deployment. The exercise record will capture its bucket,
key, S3 VersionId, SHA-256 digest, and the Terraform plan hash. The preflight
root has no permission to alter or retire that application object.

The accepted worker package checks artifact availability in its configuration
bucket. The isolated configuration bucket therefore holds a catalog mirror at
the same digest key with the same bytes, length, and `sha256` metadata. Lambda
code still comes from the exact persistent dev bucket, key, and VersionId. The
mirror is not the deployed code source and does not relax the persistent
object's read-only boundary.

Schedules and event-source mappings remain disabled outside an approved run.
A preview-first exercise command will refuse to proceed unless the AWS account,
region, deployment identity, application identity, state key, resource names,
configuration release, and private Slack destination match its saved plan.
The command will seed candidates and delivery state only through source-defined
application interfaces. Its exercise adapter must not write DynamoDB records
directly.

The first accepted load protocol is fixed before execution:

- create 5 valid synthetic deliveries per minute for 10 minutes;
- create exactly 50 deliveries, representing a 300-per-hour arrival rate over
  the exercise window;
- stop after 10 minutes even when the outcome is quiet or incomplete;
- record throughput, end-to-end duration, Lambda concurrency, DynamoDB
  throttles, queue age, outbox age, destination pacing, Slack response classes,
  both DLQs, delivery-unknown records, and alarm transitions.

The first accepted recovery protocol creates two isolated cases through the
same source-defined interfaces:

1. A due `pending_queue` record must pass through reconciler, dispatcher,
   worker, Slack, and the conditional `posted` transition.
2. An expired `sending` record must pass through reconciler to
   `delivery_unknown` without another Slack request.

The runner will emit a bounded evidence artifact that identifies the exact
cohort and distinguishes `passed`, `failed`, and `incomplete`. Evidence from a
partial run remains evidence of that partial run. It does not satisfy the
whole protocol.

After evidence capture, cleanup uses a separately saved and reviewed Terraform
destroy plan limited to the preflight state and exact preflight resource
inventory. Versioned preflight buckets require an exact, preflight-only
retirement step before Terraform can remove them. The runner will refuse any
cleanup plan that names a persistent dev resource or the shared immutable
application object.

## Failure semantics

- Any identity, plan-hash, state-key, artifact, release, or resource mismatch
  stops the run before generation begins.
- The generator stops on the first unexpected state transition and records the
  remaining cohort as not attempted.
- A Slack outcome that cannot be proved becomes `delivery_unknown` in the
  isolated table. The runner records it and does not replay it automatically.
- Missing metrics, logs, alarm history, or terminal records make the affected
  case incomplete.
- A failed cleanup disables all preflight triggers, retains state and evidence,
  and reports the exact remaining inventory. It does not widen deletion scope.
- Any observed write to a persistent dev resource invalidates the exercise and
  requires incident review before another run.

## Consequences

- Live recovery and declared-envelope evidence no longer requires synthetic
  records in the persistent delivery system of record.
- Slack side effects are limited to a private test destination with separate
  credentials.
- The exact application bytes are exercised against real AWS services, while
  the mutable state and operational signals remain disposable.
- The repository gains another Terraform root and bootstrap permission
  surface. Its state contract, resource ownership, and retirement behavior
  require the same review as the persistent roots.
- A 10-minute run proves behavior only for the declared window and cohort. It
  does not establish long-duration capacity or Slack availability.
- Shared Terraform module extraction may reduce drift, but the implementation
  must prove parity. This decision does not require a specific module layout.

## Options considered

### Seed the persistent dev deployment

This would use the deployed path with the least new infrastructure. It would
mix synthetic records into the delivery system of record and send load traffic
to the operational Slack destination. It was rejected.

### Use local tests and the existing live cohort

The current tests and 12-message cohort are useful evidence. They do not
exercise live recovery transitions or the declared 300-per-hour arrival rate.
This option was rejected as the terminal L-09 proof.

### Instantiate a second copy of the central root

The central root currently assumes persistent dev state and resources. A
second instance would need exceptions around its backend, deployment guard,
configuration, and shared account resources. Those exceptions would make the
exercise boundary difficult to review. This option was rejected.

### Build a dedicated isolated preflight root

This adds infrastructure and cleanup work. It gives the exercise an exact
state boundary, private destination, fixed cohort, and explicit terminal
conditions while preserving the real application artifact. This is the
proposed choice.

## Verification

- Prove the preflight and persistent dev backends use different exact state
  keys and that their resource inventories do not overlap.
- Prove the preflight functions reference the same immutable S3 object version
  and SHA-256 application digest as the persistent dev functions.
- Simulate the preflight roles against their exact tables, indexes, queues,
  logs, configuration versions, secret reference, and application object.
- Invert every account, region, deployment, state-key, artifact, release,
  destination, and cleanup guard and prove the command refuses the mutation.
- Run the fixed 50-message load protocol once and retain every required metric
  and terminal delivery record without extending the window.
- Run both recovery cases and prove the expected state paths and Slack request
  counts.
- Prove the persistent dev tables, queues, Slack destination, alarms, and
  Terraform state did not change during the exercise.
- Review and apply the exact cleanup plan, then prove no preflight triggers,
  resources, configuration versions, or credentials remain. Retain the state
  history and evidence artifact according to the repository evidence policy.

## Migration and rollback

No persistent dev data migrates. Implementation adds the preflight state and
resources disabled by default.

Rollback stops generation, disables every preflight trigger, records the exact
remaining state, and applies only a reviewed destroy plan for that state. If
safe cleanup cannot be proved, the resources remain disabled until a corrected
plan is reviewed. The persistent deployment and shared immutable application
object are unchanged.

## Revisit conditions

Revisit this decision if the first fixed run shows that 10 minutes cannot
produce the required metrics, if the supported arrival envelope changes, if a
private Slack destination cannot be provisioned with separate credentials, if
the preflight root cannot avoid a persistent-resource permission, or if the
cleanup proof cannot enumerate every versioned object and mutable resource.

## References

- [ADR-007: Durable outbox and Slack worker](007-central-slack-delivery-queue-and-worker.md)
- [ADR-010: Operations and supported scale](010-operations-and-supported-scale.md)
- [ADR-016: Production preflight and event contracts](016-production-preflight-and-event-contracts.md)
- [Acceptance and implementation sequence](../architecture/specification/06-acceptance-and-generation.md)
- [Operations runbook](../runbooks/operations.md)
