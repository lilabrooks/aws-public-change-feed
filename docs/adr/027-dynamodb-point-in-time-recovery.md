# ADR-027: DynamoDB point-in-time recovery for both state tables

- Status: Accepted
- Date: 2026-09-03
- Owner: Lila Brooks
- Relates to: [ADR-007](007-central-slack-delivery-queue-and-worker.md), [ADR-016](016-production-preflight-and-event-contracts.md), [ADR-025](025-source-state-and-response-page-retirement.md)

[ADR-028](028-separate-cloudtrail-evidence-for-dynamodb-restore-identity.md)
adds the provider evidence used when an active destination no longer exposes
`RestoreSummary`.

## Context

L-41 must set one recovery contract for the source-state and delivery tables
before the production gate can use either table as durable evidence. The
delivery table owns candidate history, outbox work, pacing, attempts, and
terminal outcomes. The source-state table owns acquisition checkpoints,
announcement history, emission references, and response page proofs.

Before this decision, both tables enabled DynamoDB TTL and left point-in-time
recovery disabled by default. ADR-025 retains announcement and response-page records for
730 days, but it makes no recovery promise after DynamoDB deletes an eligible
record. Raw response bytes remain executable for 30 days. SQS is transport and
must be rebuilt from the durable delivery state rather than treated as a
backup.

The owner selected PITR for both tables with the full 35-day recovery period.
The current table sizes could not be read during the decision because no AWS
credentials were available. Cost is therefore expressed as a checked regional
rate and a formula, not a current bill estimate.

## Decision drivers

- Recover recent accidental writes and deletes in either system-of-record
  table.
- Keep source and delivery state at one recovery timestamp so cross-table
  references can be checked without inventing chronology.
- Preserve `delivery_unknown` and every audited replay boundary after restore.
- Keep the original tables available until the restored pair and runtime
  cutover have passed exact read-back.
- Give a failed or late exercise an explicit incomplete result.
- Avoid claiming that PITR extends the 30-day executable replay horizon or the
  730-day TTL policy.

## Decision

Enable DynamoDB PITR on both tables with a 35-day recovery period. A shorter
period has the same PITR storage price and would remove recovery points.

Enable DynamoDB deletion protection on both Terraform-owned primary tables.
The shared central module disables it only when `preflight_mode=true`, because
the isolated ADR-024 stack must remain disposable through its reviewed destroy
plan. Restored tables stay outside this primary-table guard and keep their
separate exact-name cleanup authorization.

Use these production recovery objectives for the pair:

| Objective | Boundary |
| --- | --- |
| Recovery point | Restore both tables to the latest shared timestamp allowed by their provider-reported windows and measure it against a nominal 5-minute target. |
| Recovery time | Complete restore, repair, verification, cutover, and rollback readiness within 4 hours of the authorized recovery start. |
| PITR history | 35 days for both tables. |
| Executable source replay | 30 days, still governed by raw-snapshot retention and exact release availability. |
| Source audit and dedupe history | 730-day TTL policy; PITR gives no recovery promise after a deletion falls outside its 35-day window. |
| Original-table hold | Keep both original tables until the restored pair has passed the production gate or an incident-specific owner decision authorizes retirement. |

### Mechanism and proof boundary

The recovery mechanism has six recorded stages. Each answers a different
operational question, and evidence from one stage cannot stand in for a later
one.

| Stage | Required evidence | Boundary |
| --- | --- | --- |
| Restore request | The digest-bound plan names both primary ARNs, one shared UTC restore point, and two new destination names. The immediate response supplies an exact `RestoreSummary`; ADR-028 CloudTrail evidence supplies the same identity after that optional field disappears. | This proves which provider calls were accepted. It does not prove that either destination is complete or contains the expected state. |
| Restored-table verification | Both destinations are `ACTIVE`; schema, billing, encryption, GSI, TTL, 35-day PITR, and tags pass read-back; complete strongly consistent inventories satisfy the protected and TTL-eligible cohort rules. | Only this stage may report `restore_stage_status=completed`. The command still reports `exercise_status=incomplete_pending_cutover_rollback_and_trigger_restoration`. |
| Cutover | One saved Terraform plan selects the exact restored pair and recovery-plan digest while all four trigger requests remain false and watcher execution stays paused. Read-back covers Lambda environment variables, table and index IAM resources, alarms, dashboard dimensions, and outputs. | The proof records a coherent runtime binding with zero application writes. The guard cannot enable a trigger against the restored pair. |
| Rollback | A second saved Terraform plan clears the recovery input before any restored-table trigger runs. Read-back proves that every binding has returned to the retained primary pair. | This avoids two writable histories. Any later incident promotion of a restored pair needs another accepted decision. |
| Trigger restoration | The operator restores the four trigger states and watcher concurrency recorded before quiescence, after the primary bindings pass read-back. | Service restart is evidence in its own right; rollback completion does not establish it. |
| Cleanup | A separate authorization names each temporary table and ARN, removes it through an identity-checked path, and records the result. | The permanent recovery role has no `DeleteTable` permission. Failed, partial, and abandoned targets remain visible until this cleanup occurs. |

L-41's completion boundary requires one fresh end-to-end proof that records
these stage outcomes and the exact cleanup result. A successful restore request
or a completed restore stage alone cannot satisfy it.

### Trust and safety boundaries

- Quiescence stops watcher, dispatcher, worker, and reconciler triggers and
  pauses watcher execution before the source inventories are bound. The worker
  drains accepted FIFO work first.
- The recovery role can start restores from the two primary tables, inspect
  both pairs, and repair TTL, PITR, and tags on restore-name prefixes. Its
  primary-table access is read-only, the proof command calls no item-write API,
  and the role cannot delete a table.
- ADR-028 keeps `cloudtrail:LookupEvents` in a separate evidence role. AWS
  requires that permission on `*`, so the role can see recent management events
  outside this service. The command retains only the two bounded event
  projections and their raw-event hashes.
- Inventory verification is complete within reviewed caps. Reaching 100,000
  items or 256 MiB is a refusal; a sample cannot qualify a table for cutover.
- SQS remains transport. The operator reviews restored `queued`, `sending`, and
  `delivery_unknown` records before rebuilding queue work, and neither restore
  nor rollback authorizes an automatic resend.
- PITR recovers recent DynamoDB state. Raw-snapshot retention still sets the
  30-day executable source-replay window, and DynamoDB TTL can remove eligible
  records from a restored table.

The 5-minute value is a nominal operator target derived from DynamoDB's
documented latest-restorable boundary. Preview selects the earlier of the two
reported latest times, capped at the declared recovery start, instead of
requiring an operator to guess the provider boundary. It records the measured
distance and whether the nominal target was met. Provider lag beyond five
minutes does not prevent testing the recovery mechanism, but the evidence
cannot claim that the nominal RPO was met. The 4-hour value is a service
target. DynamoDB does not guarantee a fixed restore duration, so a miss
produces an incomplete result rather than a provider-failure claim.

### Restore plan

One canonical preview binds:

- Git SHA, deployment ID, account, Region, scoped recovery role and caller
  session, and the captured Terraform output file path and SHA-256;
- both source table ARNs and table IDs;
- the provider-derived shared restore timestamp, each table's earliest and
  latest restorable times, and the measured nominal-RPO result;
- key schemas, billing modes, encryption settings, GSIs, TTL settings, PITR
  settings, tags, item counts, and reported byte sizes;
- exact new destination names, the decision ID, operator, start time, and the
  4-hour deadline;
- complete bounded inventories with order-independent digests of every source
  item and a separate set of items already TTL-eligible by the recovery
  deadline; and
- trigger, Lambda, queue, and runtime table-binding state.

Apply re-reads the bound local files and live AWS controls before the first
restore call. The captured Terraform output check proves file integrity, while
live Lambda, EventBridge, SQS, table, and STS reads prove the relevant applied
state. It refuses a changed table identity, a restore time outside the fresh
PITR window, changed protected source or runtime state, a conflicting
destination identity, or plan digest. It starts both restores against the same
UTC timestamp and records the bounded result for each destination.
An accepted request is an in-progress result, not proof of a restored table.

### Quiescence and SQS boundary

For the controlled proof, stop watcher acquisition and dispatcher and
reconciler scheduling first. Let the worker drain accepted FIFO work, then
disable its event source. Set watcher reserved concurrency to zero and wait one
full watcher timeout. Record all three approximate queue counters at zero, no actionable delivery state, and
no live sending lease. Wait until both tables report a latest restorable time
after that quiescence boundary. Preview then derives the latest timestamp
shared by both reported windows.

An incident may make a clean drain impossible. In that case, record every
restored `queued`, `sending`, and `delivery_unknown` item before rebuilding
transport. Expired `sending` work follows ADR-007 into `delivery_unknown`.
`queued` work receives operator review because the restored table cannot prove
whether its old SQS message survived. No recovery step automatically resends
either class.

### Restored-table verification

DynamoDB restores into new table names. Before cutover, reapply and read back
TTL on `expires_at`, PITR with the 35-day period, project tags, alarms, and exact
runtime IAM resources. Verify the source-state key schema and the delivery
table's `status-next-action-index`, billing mode, and encryption settings.

Run a complete, strongly consistent inventory while both restored tables are
quiescent. The inventory has a reviewed maximum item and byte count; reaching
either cap makes the result incomplete. Validate item types, key bytes, and
delivery-state counts against the source inventory bound by preview. Items
whose numeric `expires_at` is later than the recovery deadline, or is absent or
malformed, form the protected cohort and must match exactly. Items already
TTL-eligible by the deadline form a separately recorded digest set; the later
source and destination sets may only be subsets of the preview set. A changed
TTL-eligible item is not accepted as a deletion.

AWS documents target-table read and write actions as dependencies of
`RestoreTableToPointInTime`. The recovery role grants those actions only on the
two exact restore-name prefixes. It grants no item-write action on either primary and
no `DeleteTable` action. The proof tool never invokes an item-write API.

### Cutover and rollback

Terraform keeps the original tables under their existing resource addresses.
A separate exact recovery input changes all runtime table environment values,
IAM table and index resources, alarms, dashboard dimensions, and outputs to the
restored names as one disabled-trigger apply. Exact read-back must show that
the watcher uses the restored source and delivery tables and that dispatcher,
worker, and reconciler use the restored delivery table and index.

Before triggers resume, rollback clears that recovery input and returns every
runtime binding and IAM resource to the original pair. This proves rollback
without reconciling two writable histories. Once any restored-table trigger
resumes, automatic rollback is forbidden. A later return to the original pair
needs a new recovery plan that accounts for every write since cutover.

The controlled proof leaves all runtime triggers disabled after cutover
read-back, rolls back to the original tables, proves the original bindings,
and only then restores the trigger states recorded before the exercise. It
records zero application item writes in both restored tables and both original
tables during the disabled cutover window. DynamoDB TTL deletion of an item
classified as eligible by the bound deadline is recorded separately and does
not invalidate that claim. Destination-table deletion remains a separately
authorized cleanup action against exact names and ARNs.

The Terraform cutover guard intentionally permits only the controlled proof
topology: every trigger request remains disabled while a recovery input is
selected. Promoting a restored pair to live incident service would require a
separate accepted decision and a guard change; ADR-027 does not authorize that
transition.

## Failure semantics

- A restore timestamp outside either reported PITR window stops before a
  provider mutation.
- One accepted restore and one refused restore produces a partial result. The
  accepted destination remains recorded for review and cleanup.
- A timeout or unknown restore response is reread by exact destination name.
  An unresolved outcome is ambiguous.
- A restored table that is not `ACTIVE`, misses a required setting, exceeds an
  inventory cap, or fails an invariant remains ineligible for cutover.
- A provider-derived recovery point more than 5 minutes before the start is
  recorded as a nominal-RPO miss. The recovery mechanism may still be tested,
  but the evidence cannot claim that objective was met. A 4-hour recovery-time
  miss makes the restore stage incomplete even when both tables eventually
  restore.
- Any application write during the disabled cutover window invalidates the
  zero-write claim and stops the exercise. A deletion from the bound
  TTL-eligible cohort is classified separately; any protected-item change
  still stops it.
- Failure after runtime rebinding leaves every trigger disabled. The operator
  either proves the restored pair or rolls bindings back before restoring
  triggers.
- PITR does not resolve Slack delivery ambiguity. Restored unknown outcomes
  keep their existing manual-review rules.

## Cost

For `us-east-1`, the checked public rate is $0.20 per GB-month for PITR and
$0.15 per GB restored. The recurring estimate is the average combined billable
size of both tables multiplied by $0.20. One paired restore is the combined
restored size multiplied by $0.15, plus ordinary storage and request charges
for the temporary tables while they exist.

The live table sizes and billing overhead remain unknown. Preview records
`TableSizeBytes` as operational context, but the evidence must state that AWS
billing includes storage overhead absent from that value.

## Options considered

### Enable PITR on both tables for 35 days

This is the owner's selected direction. It protects recent source and delivery
state under one recovery mechanism and keeps the two-table exercise symmetric.

### Enable PITR only on the delivery table

This protects outbox and outcome history, but leaves recent feed checkpoints,
announcement history, and response-page proof without the same recovery
timestamp. Rebuilding source state from public feeds cannot reproduce removed
items or the exact observed bytes.

### Keep PITR disabled

This accepts loss after a table deletion or corruption. Public feeds can
rebuild some acquisition state, and retained S3 bytes can replay 30 days, but
neither path reconstructs every delivery attempt or Slack outcome. That loss
boundary is too wide for the production gate.

### Use scheduled on-demand backups

Scheduled backups can provide longer retention, but their recovery points are
coarser and they add scheduling and retention policy. They remain a possible
archive decision after PITR; they do not replace the selected recent-corruption
boundary.

## Consequences

PITR adds a size-based monthly charge for both tables and a restore charge when
the proof or an incident performs a restore. Both original tables stay present
during proof, so temporary storage also increases.

The infrastructure needs a recovery-period setting, exact restored-table
runtime bindings, scoped restore authority, and preview-first restore tooling.
The runbook must cover quiescence, restore progress, setting repair, invariant
inventory, disabled-trigger cutover, rollback, and cleanup.

PITR protects at most 35 days. The 730-day source-state TTL policy remains an
audit and dedupe policy rather than a 730-day disaster-recovery promise.

## Implemented work and checked evidence

The repository implements the decision across these owned surfaces:

- [`infra/central/dynamodb.tf`](../../infra/central/dynamodb.tf) enables
  35-day PITR on both primary tables and
  rejects a recovery cutover unless PITR, stopped triggers, paused watcher
  execution, exact restore-name prefixes, and one shared exercise ID all hold.
- `infra/central/locals.tf`, `lambda.tf`, `iam.tf`, `alarms.tf`,
  `dashboard.tf`, and `outputs.tf` derive runtime table and index references
  from the selected pair. The primary resources remain under Terraform
  ownership for rollback.
- [`infra/central/iam.tf`](../../infra/central/iam.tf) defines the scoped
  recovery role and the separate
  CloudTrail-only evidence role. The latter has one service action:
  `cloudtrail:LookupEvents`.
- [`prove_dynamodb_recovery.py`](../../scripts/prove_dynamodb_recovery.py) owns
  `preview`, `apply`, `evidence`, and
  `status`. Canonical plan and evidence files have caller-supplied expected
  SHA-256 values, and the command returns bounded refused, partial, ambiguous,
  incomplete, or completed results.
- [`test_dynamodb_recovery_proof.py`](../../tests/test_dynamodb_recovery_proof.py)
  covers plan and evidence identity,
  shared timestamps, quiescence, inventory caps and TTL cohorts, restore
  outcomes, settings repair, the recovery clock, and the distinction between
  restore-stage and exercise status.
  [`test_terraform_contracts.py`](../../tests/test_terraform_contracts.py)
  checks the 35-day defaults, deletion protection and preflight disposal, IAM
  limits, all runtime consumers, provider-free cutover refusals, and return to
  primary bindings.

The first live attempt, `l41-20260905t162330z`, created both planned tables.
Its immediate responses contained the expected restore summaries; later active
table descriptions omitted them. The command stopped before settings repair,
cutover, or restored-table runtime writes, and the original bindings and
triggers were restored. ADR-028 records the observed provider behavior and the
new evidence path. Its accepted implementation cannot retroactively complete
that attempt because the old plan and verifier bind an earlier Git identity.

The fresh dev exercise completed on 2026-09-05 against commit `3bf35b7`.
Recovery plan SHA-256
`aa0f10e27f977c0f04ab7f3b8faa5ecdbc5222fad3e28cebbacd2dbe763a9a25`
and recovery evidence SHA-256
`011258c9c89185ac94ead6afb6bc3f152d1f80d8bfc6c41ac8444d109cdc4899`
bind the provider calls and verified state. The shared restore point was 298
seconds behind the declared start, so it met the nominal 5-minute target. Both
destinations passed complete inventory, schema, tag, TTL, and 35-day PITR
checks before the 4-hour deadline.

With all triggers disabled, the saved cutover plan moved every runtime, IAM,
alarm, dashboard, and output reference to the restored pair. The saved rollback
plan returned those references to the primary pair before any restored-table
runtime write. The inventory comparison passed again, all four triggers and
watcher concurrency were restored, all seven relevant alarms reached `OK` with
actions enabled, and the final Terraform plan reported no changes at
`2026-09-05T18:55:41Z`.

The repository owner then authorized deletion of all four disposable restore
tables from the successful and superseded attempts. AWS waiters completed, and
exact-name reads confirmed all four absent at `2026-09-05T19:09:15Z`. The
[L-41 record](https://github.com/lilabrooks/aws-public-change-feed/issues/146)
preserves the live proof in comment `5554120649` and cleanup result in comment
`5554142372`. This completes L-41's dev operational proof. Production
readiness remains a later M3 decision, and one successful exercise cannot
guarantee future provider timing.

L-50 completed on 2026-09-05 against commit
`4833343fd99a8938f12ee2c71854780490c5193c`. Saved central plan SHA-256
`2bc02f0d47437a67b1be1546ab9dab5c5c88859f147bc5437d2507251d74986c`
contained exactly two in-place updates, changing only
`deletion_protection_enabled` from `false` to `true` on the primary tables.
The owner authorized those exact bytes. Terraform reported 0 added, 2 changed,
and 0 destroyed. Direct DynamoDB reads returned both tables `ACTIVE` with
deletion protection enabled. Fresh central plan SHA-256
`4a399d5db77a5487b2b33b89e69c867fc9f9be1245522b9aee778f8216ea1b3d`
reported no changes. Isolated preflight plan SHA-256
`b0e1de2f168ca1302d791b86fb6270c0d8972147baf04c00df1545bd2560a079`
planned both tables with deletion protection disabled; it was not applied.

## Verification

- Invert every bound plan identity and prove the restore calls do not run.
- Prove both restore calls use one timestamp and exact destination names.
- Exercise accepted, refused, partial, timed-out, and ambiguous provider
  outcomes with bounded results.
- Prove restored TTL, PITR, tags, schema, GSI, encryption, and billing settings
  through exact read-back.
- Invert each item invariant and show that cutover is refused.
- Prove all runtime table names, IAM resources, alarms, dashboard dimensions,
  and outputs move together and return together.
- Prove triggers remain disabled after every failed or incomplete cutover.
- Record the RPO and RTO clocks independently from eventual provider success.
- Prove the machine result calls restore verification a stage and always marks
  the overall exercise pending until external cutover, rollback, and trigger
  restoration evidence is recorded.
- Run the controlled restore only under a separately authorized plan and keep
  its exact cleanup evidence.

## Migration and rollback

Enable PITR and wait until both tables expose the accepted recovery window
before scheduling the proof. This change has no item migration.

The restore exercise creates new tables and never restores over an existing
name. Before trigger resumption, rollback returns runtime bindings to the
original pair. After trigger resumption, a second cutover is a new recovery
operation because the restored pair may contain writes absent from the
original tables.

Disabling PITR resets the recoverable history if it is enabled again later.
That change needs its own owner decision. Removing recovery tooling leaves
existing backup history and restored tables subject to their provider
lifecycle and separately reviewed cleanup.

## Revisit conditions

- Either table's billable size makes PITR cost exceed the accepted operating
  budget.
- A measured restore cannot meet the 4-hour target.
- The service needs recovery older than 35 days or across Regions.
- Inventory cannot validate all restored items inside its reviewed caps.
- A real incident shows that the quiescent SQS procedure loses or repeats work.
- The production topology adds another table or changes state ownership.

## References

References verified: 2026-09-05.

- [L-41: Decide and prove the production data-recovery objective](https://github.com/lilabrooks/aws-public-change-feed/issues/146)
- [DynamoDB disaster-recovery strategies](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamodbDisasterRecoveryStrategy.html)
- [Point-in-time recovery](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Point-in-time-recovery.html)
- [Restore a table](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/pointintimerecovery_restores.html)
- [Using IAM with DynamoDB backup and restore](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/backuprestore_IAM.html)
- [DynamoDB actions and dependent permissions](https://docs.aws.amazon.com/service-authorization/latest/reference/list_dynamodb.html)
- [DynamoDB pricing](https://aws.amazon.com/dynamodb/pricing/)
- [DynamoDB billing and usage reports](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-understanding-billing.html)
