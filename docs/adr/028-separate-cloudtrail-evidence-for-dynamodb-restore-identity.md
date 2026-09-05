# ADR-028: Separate CloudTrail evidence for DynamoDB restore identity

- Status: Accepted
- Date: 2026-09-05
- Owner: Lila Brooks
- Relates to: [ADR-016](016-production-preflight-and-event-contracts.md), [ADR-024](024-isolated-live-runtime-exercises.md), [ADR-027](027-dynamodb-point-in-time-recovery.md)

## Context

ADR-027 requires an existing recovery destination to prove the exact source
table ARN and restore timestamp before the recovery command may reuse it. The
first live L-41 restore started both exact destinations from the authorized
plan. While each table was `CREATING`, DynamoDB returned a `RestoreSummary`
with that identity. Once both tables became `ACTIVE`, `DescribeTable` omitted
the summary.

The recovery command treated the omission as a conflicting destination. Both
tables were active and complete when scanned, but the command could no longer
prove how they were created. The exercise stopped before settings repair,
cutover, or restored-table writes. Its original runtime bindings and triggers
were restored. The two recovery tables and the plan evidence remain for
separately authorized cleanup.

AWS documents `RestoreSummary` as an optional `DescribeTable` response field.
It does not document how long the field remains present. The observed response
therefore cannot support ADR-027's assumption that the summary remains
available throughout an active table's verification.

CloudTrail Event history records management events in the Region for 90 days,
is enabled by default, and cannot be manually deleted. The two live
`RestoreTableToPointInTime` calls appeared there with their event IDs, exact
source ARNs, target names, restore timestamp, caller role, and absence of a
provider error. Both events had `responseElements=null` and an empty resource
list, so CloudTrail supplied no target ARN. `LookupEvents` permits only one
attribute filter, is limited to two requests per second per account and
Region, and does not support resource-scoped IAM permission.

## Decision drivers

- Prove the provider operation after `RestoreSummary` disappears.
- Bind evidence to the authorized recovery plan, exact caller, source ARN,
  target name, restore timestamp, account, and Region.
- Keep CloudTrail's account-wide management-event visibility out of the role
  that can start restores and repair destination settings.
- Fail closed when event delivery is late, absent, duplicated, malformed, or
  outside the recovery clock.
- Keep provider responses and unrelated account activity out of operator
  output and repository history.
- Preserve the four-hour recovery-time boundary and the requirement for a
  fresh, separately authorized restore plan.

## Decision

Add a separate `dynamodb_recovery_evidence` operator role. Its only service
permission is `cloudtrail:LookupEvents` on `*`, because the API has no
resource-level permission. It receives no DynamoDB, Lambda, EventBridge, SQS,
S3, secret, write, delete, or role-assumption permission. The existing
`dynamodb_recovery` role and its policy remain unchanged.

Add an evidence action to the recovery proof command. It runs under the
evidence role and accepts only an unchanged canonical recovery plan and its
expected SHA-256. It queries the plan's Region and the time range from
`started_at` through `deadline_at`. Because CloudTrail accepts one attribute
filter, the command filters on `RestoreTableToPointInTime`, paginates within a
fixed event and page cap, and checks the returned event payloads locally.

For each planned table, exactly one event must satisfy all of these conditions:

- event source is `dynamodb.amazonaws.com` and the event is a write;
- account and Region equal the plan;
- the assumed-role session and its issuer equal the recovery identity bound by
  the plan;
- source table ARN, target table name, restore timestamp, and
  `PAY_PER_REQUEST` billing override equal the plan;
- event time falls inside the plan's recovery clock;
- the event has no provider error code or message; and
- the requested target table name identifies the planned destination.

The action writes a new canonical evidence file only after both events pass.
The file binds its format version, decision, recovery-plan digest, evidence
role session, capture time, account, Region, verifier Git SHA, and a bounded
projection of each validated event. The projection includes the event ID,
event time, request ID, recovery-role session, source ARN, target name, the
expected ARN derived from the source ARN and target name, restore timestamp,
and billing override. It also records a SHA-256 of the raw
event but does not retain the raw event, access-key identifier, network
address, user agent, or unrelated account events.

The file path must not exist before capture. Apply and status accept it only
with an expected evidence SHA-256 and reject changed or noncanonical bytes.
`RestoreSummary` remains useful for the immediate response and while a table is
creating. Once an existing destination omits that summary, the exact CloudTrail
evidence file is mandatory before configuration or verification may continue.
An active table passes only when its event identity passes, its live ARN equals
the derived expected ARN, and its settings and complete inventory pass their
existing checks.

CloudTrail evidence proves the restore request. It does not prove table
contents, successful settings repair, cutover, rollback, zero restored-table
writes, trigger restoration, or cleanup. Those ADR-027 checks remain separate.

## Current exercise disposition

The `l41-20260905t162330z` attempt remains incomplete. Its verifier and plan
were produced by the code version that assumed persistent `RestoreSummary`.
ADR-028 does not reinterpret that result or relax its Git identity. After this
decision is accepted and implemented, L-41 uses a fresh exercise ID, fresh
targets, fresh capture, and a newly authorized plan.

The current restored tables are not deleted as part of this decision. Their
exact cleanup remains a separate live mutation.

## Failure semantics

- Missing events produce an incomplete evidence result. The operator may retry
  the same read-only capture within the plan deadline; no restore call runs.
- A malformed event, a field mismatch, more than one exact match, pagination
  beyond the fixed cap, or a changed evidence file refuses verification.
- CloudTrail throttling or an uncertain lookup result is ambiguous. It does
  not become evidence through retry inside the command.
- Evidence that first becomes available after the deadline is retained as
  audit material, but it cannot turn the restore stage into a pass.
- One proven restore and one missing or conflicting event leave the pair
  incomplete and ineligible for cutover.
- A live destination whose ARN differs from the event remains a target
  conflict even if its name matches.
- Failure after a restore leaves runtime triggers stopped until the original
  bindings are proved and a separately authorized restart plan is applied.

## Options considered

### Add CloudTrail lookup to the DynamoDB recovery role

This is the smallest implementation change, but `LookupEvents` exposes recent
management activity for the whole account and Region. Combining that read
scope with restore and destination-repair authority makes the recovery session
broader than its task requires.

### Use a separate CloudTrail evidence role

This adds one role and one explicit handoff. It keeps the mutating recovery
session unchanged and makes the account-wide read boundary visible in
Terraform, command identity checks, and the runbook. This is the selected
choice.

### Store restore identity in DynamoDB tags

DynamoDB does not copy source tags to a restored table, so the recovery command
already repairs them. A proof-only identity tag could survive after the
restore, but it cannot cover a crash between the accepted restore response and
the tag write. It also lets the destination-repair role write the evidence it
later consumes and changes ADR-027's exact source-tag comparison. Tags are not
accepted as restore identity evidence.

### Store only a local operation receipt

A local receipt can preserve the response seen by one process, but a later
process cannot independently prove that the provider issued it. A receipt
without provider evidence is insufficient for cutover.

### Require every proof to finish before `RestoreSummary` disappears

The live exercise showed that the summary may disappear before the command can
repair and verify an active destination. Repeating the restore to seek a
different timing outcome would not repair the contract and would distort the
observed behavior.

## Consequences

The central root gains one permanent read-only operator role and output. The
proof command and runbook gain a two-session handoff and a second canonical
digest. Tests must cover caller isolation, event pagination and caps, exact
field inversion, missing and duplicate events, delayed evidence, changed
bytes, and ACTIVE tables without `RestoreSummary`.

The evidence role can read recent management events beyond DynamoDB because
AWS cannot scope `LookupEvents` to a service or resource. The command reduces
what it retains and prints, but IAM remains the enforceable confidentiality
boundary. Operators should assume the role only for a bounded capture and then
return to the DynamoDB recovery role.

Event history is finite and eventually available. The four-hour recovery
clock contains that dependency. A delayed event can make an otherwise complete
restore miss the exercise target without implying that DynamoDB failed.

No CloudTrail trail, event data store, tag convention, or application runtime
dependency is added.

## Verification

- Prove the evidence role allows `cloudtrail:LookupEvents` and denies every
  recovery, table, queue, function, object, secret, and write action checked by
  the existing policy tests.
- Invert each event identity field and prove no evidence file is written.
- Make an ACTIVE-table fixture omit `RestoreSummary`; prove status refuses
  without evidence and passes only with the exact canonical evidence digest.
- Force missing, duplicate, malformed, late, over-cap, throttled, and changed
  evidence conditions and check their bounded status and exit code.
- Prove an immediate restore response still requires its existing exact
  `RestoreSummary` fields.
- Trace one fresh live proof through recovery response, CloudTrail capture,
  evidence loading, live table checks, cutover, rollback, and trigger
  restoration.
- Preserve the first live exercise as the regression evidence. Do not repeat a
  quiet provider observation merely to obtain a different response.

## Migration and rollback

After acceptance, implement and test the role, output, evidence format,
command, and runbook together. Deploy them through an exact central Terraform
plan. Run a fresh L-41 proof; the prior targets remain outside the new plan.

Before another restore starts, rollback consists of removing the proposed
role, output, evidence action, and acceptance text. After an evidence file or
fresh restore exists, retain its audit material. Removing the role stops future
captures but does not alter DynamoDB tables, PITR, or CloudTrail Event history.

## Revisit conditions

- AWS documents and demonstrates a restore identity field that persists for
  the destination's lifetime.
- CloudTrail changes Event history retention, immutability, event fields,
  lookup filtering, quotas, or IAM scoping.
- Evidence delivery cannot reliably complete inside the four-hour target.
- Account policy forbids a role with `LookupEvents` on `*`.
- A future recovery service supplies a resource-scoped, durable operation
  receipt.

## References

References verified: 2026-09-05.

- [L-41: Decide and prove the production data-recovery objective](https://github.com/lilabrooks/aws-public-change-feed/issues/146)
- [DynamoDB `DescribeTable`](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_DescribeTable.html)
- [Restore a table in DynamoDB](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/pointintimerecovery_restores.html)
- [CloudTrail Event history](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/view-cloudtrail-events.html)
- [View CloudTrail events with the AWS CLI](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/view-cloudtrail-events-cli.html)
- [CloudTrail quotas](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/WhatIsCloudTrail-Limits.html)
