# ADR-025: Source-state and response-page retirement

- Status: Accepted
- Date: 2026-08-29
- Owner: Lila Brooks

## Context

The source-state table contains three classes of durable item: feed checkpoints,
announcement history, and response page-set markers. Raw feed responses live in
S3. These records serve different purposes and cannot share one retirement rule.

The current configuration retains raw snapshots for 30 days and sets a 730-day
policy for feed and announcement state. The source-state table has DynamoDB TTL
enabled on `expires_at`, but source-state writers do not populate that attribute.
Active feed checkpoints must remain available without a cleanup TTL. Response
page-set markers are immutable completion proofs and currently carry no reliable
creation or observation time.

Three M2 boundaries depend on a clear lifecycle:

- L-35 replays a retained raw response against an exact configuration release.
- CR4-10 retires source state for a feed removed from the active configuration.
- L-14 separately retires immutable configuration releases while preserving the
  releases required by replay and rollback policy.

The design must preserve the feed-name and URL invariant, keep page-set proofs
trustworthy, and avoid assigning invented ages to legacy records.

## Decision drivers

- Keep active feed checkpoints available for watcher acquisition and comparison.
- Preserve immutable response page-set proofs while their dedupe horizon remains
  in force.
- Enforce the rule that a changed feed URL receives a new feed name.
- Make replay claims match the bytes and configuration releases that still exist.
- Bound operator authority and make uncertain outcomes fail closed.
- Apply the configured retention periods without pretending DynamoDB TTL deletion
  is immediate.

## Decision

Use a hybrid lifecycle. Automatic TTL handles announcement history and response
page-set retention. A preview/apply operation handles removed-feed state. Raw
snapshot lifecycle remains an S3 concern.

### Lifecycle matrix

| Record | Retention | Retirement mechanism | Purpose after ingestion |
| --- | --- | --- | --- |
| Raw response snapshot | 30 days | S3 lifecycle | Executable replay bytes |
| Active feed checkpoint | No expiry | Watcher compare-and-set updates | Acquisition position and feed URL binding |
| Removed feed checkpoint | Full checkpoint for 730 days from the reviewed retirement decision | Bounded preview/apply, then conditional compaction | Audit and reviewed recovery window |
| Announcement history | 730 days from `last_observed_at` | DynamoDB TTL, refreshed by compare-and-set merge | Dedupe and observation history |
| Response page-set marker | 730 days from its latest exact observation | DynamoDB TTL; proof fields remain immutable | Durable completion proof for a page set |
| Retired-feed tombstone | Permanent | Created by conditional compaction | Feed-name and URL-hash reuse guard |

The table is the compact lifecycle reference. Numbered specifications own the
normative runtime requirements. Operator steps belong in the applicable runbook.

### Executable replay horizon

The 30-day raw-snapshot retention period defines the executable replay horizon.
Source state retained beyond that point supports audit and dedupe; it does not
promise that the original response can still be replayed.

An L-35 replay preview binds the snapshot object key, response-body digest, and
exact configuration release. Apply treats the plan as stale when the snapshot or
release is missing or has changed. M2 adds no durable snapshot pin. Extending the
replay horizon therefore requires a separate retention decision.

### Active feed checkpoints

Active feed checkpoints never receive `expires_at`. Their normal watcher lease,
pending-page, and compare-and-set rules remain authoritative.

### Announcement history

An announcement record receives `expires_at` equal to
`last_observed_at + announcement_state_ttl_days`. An exact merge that advances
`last_observed_at` also advances `expires_at` in the same conditional write. A
write never shortens the existing expiry.

Readers continue to treat an expired record as present until DynamoDB removes it.
TTL eligibility is not proof of deletion.

Existing announcement records have a trustworthy `last_observed_at`. Their
expiry can be derived from that value during a separately authorized migration.
The migration previews every record that is already past its derived eligibility
time before applying conditional updates.

### Response page-set markers

The page-set identity and completion payload remain immutable. Retention metadata
is separate from the proof fields. Re-observation of the exact same proof may
extend `expires_at`; a mismatch in any proof field is a conflict and leaves the
stored item unchanged.

The reader distinguishes required proof fields from optional retention metadata
so that pre-migration and post-migration records remain readable during rollout.

Legacy markers lack a trustworthy timestamp. A one-time migration assigns
`expires_at = migration_as_of + feed_state_ttl_days`. It does not infer age from
`run_id`, table order, or current feed state. Each update conditions on the exact
key and existing proof payload. A conflict stops the apply and leaves the
remaining records untouched for a new preview.

Partial TTL deletion can remove some page markers before others. The watcher must
still reconstruct the deterministic page set from the retained snapshot and
exact release when those inputs exist. The absence of a marker never converts a
different marker payload into a match.

### Removed-feed retirement

CR4-10 uses a bounded preview/apply workflow for one named feed.

Preview records:

- the exact active configuration release and its digest;
- proof that the feed name is absent from that release;
- the checkpoint key, state version, feed URL hash, and relevant content digest;
- the absence of a live lease and pending page-set work.

Removed-feed retirement does not inventory or mutate announcement history or
response page-set markers. Their TTL and one-time migration lifecycle is
independent of the exact-feed operation.

Apply pauses watcher acquisition for the named feed, strongly rereads the bound
records, and refuses a stale plan. It conditionally records `retired_at` and
`retire_after = retired_at + feed_state_ttl_days` on the full checkpoint. The
reviewed retirement decision time is the start of the 730-day period; removal
from configuration alone does not start the clock.

After `retire_after`, a second preview/apply conditionally replaces the full
checkpoint with a permanent tombstone. The tombstone contains the feed name,
feed URL hash, retirement time, decision identifier, and compaction time. It
contains no credentials or raw feed content.

A future configuration using a tombstoned name with a different URL hash is
rejected. Restoring the same name and URL requires a separately reviewed
restoration that converts the tombstone into a valid active checkpoint. An
intentional URL change continues to require a new feed name.

### Authority boundary

The watcher runtime receives no source-state deletion permission. A permanent,
operator-assumable source-state retirement role may read and conditionally
update the selected `FEED#<feed_name>` checkpoint for preview/apply and tombstone
compaction. Its permissions are limited to the source-state table and that exact
partition key supplied through the approved operation. It receives no table
scan, delivery-table, secret, or deployment mutation permissions.

Legacy announcement and response-page inventory requires a one-time migration
role because DynamoDB `Scan` cannot be restricted to partition-key prefixes.
That role may perform a bounded, attribute-projected scan of the source-state
table and conditionally update `ANNOUNCEMENT#` and `RUN#` items. It receives no
other table, secret, deployment mutation, or `DeleteItem` permission. The role
is removed after the migration result and untouched remainder are recorded.

DynamoDB performs TTL deletions for eligible announcement and page-marker items.
Neither operator role uses `DeleteItem` for the baseline design.

### Failure semantics

- Preview is read-only and produces no retirement authority on its own.
- A changed release, checkpoint version, URL hash, proof payload, lease, or
  pending-page condition makes the plan stale.
- A feed still present in the bound release is ineligible for retirement.
- Apply stops on a conditional conflict. It reports the completed and untouched
  sets without automatically retrying the remaining mutations.
- A timed-out or ambiguous write is strongly reread. An unresolved result is
  reported as ambiguous and receives no success claim.
- TTL eligibility and TTL deletion lag are reported separately.
- Missing replay bytes or a missing exact release make an L-35 plan stale.
- A tombstone is created only after the full-checkpoint retention boundary and
  exact conditional reread both succeed.

## Options considered

### A. Hybrid TTL and reviewed compaction

This is the chosen option for the accepted decision. It keeps recurring expiry
mechanical while reserving feed identity retirement for explicit review.

### B. TTL every source-state item

This would simplify storage cleanup. It would also put active acquisition state
and the feed-name reuse guard on asynchronous deletion clocks, so it is rejected.

### C. Operator preview/apply for every expired item

This would provide a reviewed record of each deletion. It adds recurring scans
and broad operator work for announcement and page-marker churn, so it is
rejected.

### D. Retain all source state permanently

This preserves the longest audit horizon. It leaves the configured retirement
policy unenforced and does not complete CR4-10, so it is rejected.

## Consequences

- Executable replay is bounded by raw-snapshot retention even though dedupe and
  audit state live longer.
- DynamoDB may return expired announcement and page-marker records until its TTL
  worker deletes them. Runtime logic must tolerate that interval.
- Permanent tombstones add a small, monotonically growing record set. Their size
  follows retired feed names rather than announcement volume.
- Legacy page markers receive a fresh 730-day period because their true ages
  cannot be reconstructed.
- Migration and retirement need bounded inventory reads and conditional writes.
- The one-time legacy migration temporarily grants table-wide, attribute-limited
  scan access because the existing key shape has no queryable item-type index.
- The baseline provides no guaranteed restore after DynamoDB completes a TTL
  deletion. Point-in-time recovery remains disabled by default and requires its
  own production decision if post-deletion recovery becomes a requirement.

## Verification

Implementation must include tests that demonstrate:

- TTL boundary calculation and compare-and-set expiry refresh;
- treatment of expired records that DynamoDB has not yet deleted;
- immutable page proofs with exact-match expiry extension and conflict refusal;
- deterministic reconstruction after partial page-marker expiry;
- legacy page-marker compatibility and the fresh migration expiry;
- migration inventory-limit refusal and removal of the temporary scan role;
- refusal when the feed is configured, leased, or has pending page work;
- refusal after a release, state version, URL hash, or plan input changes;
- conditional retirement marking and later tombstone compaction;
- strong reread handling for ambiguous updates;
- permanent tombstone enforcement for feed-name and URL-hash mismatch; and
- stale replay plans when the retained snapshot or exact release is unavailable.

Each refusal test must invert the guarded condition and show that the operation
would otherwise proceed.

## Migration and rollback

1. Add optional retention metadata support to source-state serialization and
   readers before writing any expiry attributes.
2. Add expiry calculation to new announcement merges and response page-set
   writes.
3. Add the temporary, attribute-limited migration role. Preview the existing
   announcement and legacy page-marker migration, run a separately authorized
   conditional apply, record its partial or complete result, and remove the role.
4. Add the bounded removed-feed retirement preview/apply operation and its
   permanent exact-feed role.
5. Enable tombstone compaction only after the full-checkpoint retention boundary
   can be proven from stored retirement metadata.

Before DynamoDB removes an item, rollback may conditionally remove or extend its
`expires_at`. After deletion, this ADR makes no recovery guarantee. Rolling back
the operator tooling removes future mutation authority but preserves existing
tombstones as safety records.

## Revisit triggers

- A replay requirement exceeds the 30-day raw-snapshot horizon.
- Source-state volume or TTL deletion lag breaches an operational limit.
- A legitimate feed-name restoration cannot use the reviewed restoration path.
- Production requires guaranteed recovery after TTL deletion.
- Bounded legacy migration cannot complete within the operator window.

## References

- [ADR-013: Source state and public announcement identity](013-source-state-and-public-announcement-identity.md)
- [ADR-022: Preview-first application package retirement](022-preview-first-application-package-retirement.md)
- [Platform](../architecture/specification/02-platform.md)
- [Alert processing](../architecture/specification/04-alert-processing.md)
- [Security and operations](../architecture/specification/05-security-and-operations.md)
- [DynamoDB TTL](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html)
- [Working with expired items and TTL](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ttl-expired-items.html)
- [DynamoDB fine-grained access conditions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/specifying-conditions.html)

References verified: 2026-08-29
