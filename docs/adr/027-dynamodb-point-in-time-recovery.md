# ADR-027: DynamoDB point-in-time recovery for both state tables

- Status: Accepted
- Date: 2026-09-03
- Owner: Lila Brooks
- Relates to: [ADR-007](007-central-slack-delivery-queue-and-worker.md), [ADR-016](016-production-preflight-and-event-contracts.md), [ADR-025](025-source-state-and-response-page-retirement.md)

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

Use these production recovery objectives for the pair:

| Objective | Boundary |
| --- | --- |
| Recovery point | Restore both tables to the latest shared timestamp allowed by their provider-reported windows and measure it against a nominal 5-minute target. |
| Recovery time | Complete restore, repair, verification, cutover, and rollback readiness within 4 hours of the authorized recovery start. |
| PITR history | 35 days for both tables. |
| Executable source replay | 30 days, still governed by raw-snapshot retention and exact release availability. |
| Source audit and dedupe history | 730-day TTL policy; PITR gives no recovery promise after a deletion falls outside its 35-day window. |
| Original-table hold | Keep both original tables until the restored pair has passed the production gate or an incident-specific owner decision authorizes retirement. |

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

References verified: 2026-09-03.

- [L-41: Decide and prove the production data-recovery objective](https://github.com/lilabrooks/aws-public-change-feed/issues/146)
- [DynamoDB disaster-recovery strategies](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamodbDisasterRecoveryStrategy.html)
- [Point-in-time recovery](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Point-in-time-recovery.html)
- [Restore a table](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/pointintimerecovery_restores.html)
- [Using IAM with DynamoDB backup and restore](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/backuprestore_IAM.html)
- [DynamoDB actions and dependent permissions](https://docs.aws.amazon.com/service-authorization/latest/reference/list_dynamodb.html)
- [DynamoDB pricing](https://aws.amazon.com/dynamodb/pricing/)
- [DynamoDB billing and usage reports](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-understanding-billing.html)
