# ADR-022: Preview-first application package retirement

- Status: Accepted
- Date: 2026-08-14
- Owner: Lila Brooks

## Context

ADR-020 identifies each deployable Lambda package by the SHA-256 digest of its
exact ZIP bytes. Publication creates one versioned S3 object at
`<top-prefix>/application-artifacts/<digest>.zip`, verifies the returned
version, and never replaces existing bytes. Terraform deploys the exact digest
and S3 version.

ADR-020 also fixes 2 retention floors. Every package remains available for at
least 400 days, and the newest 10 packages remain available regardless of age.
The repository currently has no retirement action that can prove both floors,
so packages accumulate.

An S3 lifecycle rule cannot count packages or protect the newest 10. It would
also act on object age without knowing the package references selected for a
deployment or rollback. Permanent deletion from a versioned bucket requires an
exact `VersionId`; a key-only delete would add a delete marker and leave the
package bytes stored.

The existing release-publisher role can create and verify package objects. Its
policy deliberately omits deletion. Giving that role permanent deletion would
combine append-only publication with retirement and make an accidental or
compromised publisher more destructive.

Package retirement, immutable configuration-release retirement, and source
response-page retirement have separate authorities and proof seams. This
decision covers application packages only.

## Decision drivers

- Prove both accepted retention floors before each permanent deletion.
- Protect exact packages selected for active deployment, rollout, or rollback.
- Bind apply to the complete inventory reviewed during preview.
- Keep publication credentials unable to delete packages.
- Make partial and uncertain provider outcomes visible.
- Keep enumeration, output, and operator work finite.
- Permit restoration only from approved exact package bytes.

## Decision

We will add one operator-only, preview-first application-package retirement
tool and a separate application-artifact-retirement role.

### Inventory contract

The operator supplies:

- the reviewed `deployment.yaml`, from which the tool derives the exact bucket,
  application-artifact prefix, retention days, and newest-package floor;
- a positive maximum inventory count for that run;
- every protected digest and exact S3 `VersionId` selected for active
  deployment, rollout, or rollback, or an explicit assertion that the
  deployment has no protected package;
- a path for the canonical preview plan.

Preview lists object versions through the complete exact prefix. It stops when
the service returns more records than the operator's maximum. Truncated or
failed pagination never produces an applicable plan.

The deployment document must pass its owned schema. The tool refuses a
retention value below 400 days or a newest-package floor below 10, even if a
weaker document passes an older schema. The plan records the deployment bytes'
SHA-256 and every extracted value.

One valid package record has all of these properties:

- its key is exactly `<prefix>/<64 lowercase hexadecimal characters>.zip`;
- the digest metadata equals the digest in the key;
- it has one data version and no delete marker or noncurrent version history;
- that data version is current and has a nonempty S3 `VersionId`, ETag,
  `LastModified`, and size;
- each protected digest and version names that exact current record.

Any other entry under the prefix makes the inventory ineligible. The tool
reports a bounded refusal and deletes nothing. It does not clean up malformed
keys, delete markers, extra versions, or unrelated objects as incidental work.

### Retention classification

Preview captures one UTC `as_of` timestamp. A package is age-eligible only when
`LastModified <= as_of - retention_days`.

Packages are ordered from newest to oldest by `LastModified`. Every package at
or newer than the timestamp of the package in the configured floor position is
retained. This keeps all ties at the boundary because S3 timestamps cannot
prove an order within a tie. If fewer packages exist, all are retained.

Every explicitly protected digest/version pair is retained. A package becomes
a deletion candidate only when it is age-eligible, outside the newest floor and
its ties, and absent from the protected set.

The canonical plan records the deployment digest, bucket, prefix, `as_of`,
policy inputs, inventory limit, protected references, every inventory row, and
the exact deletion candidates. Rows are sorted by key and version. The tool
writes canonical JSON and reports its SHA-256. Preview performs no S3 mutation.

### Apply and conflict handling

Apply requires the same deployment document, saved plan, and expected plan
SHA-256. It verifies the deployment digest, repeats the complete inventory with
the same limit, and refuses when any row or protected reference differs. A new
publication, restored version, delete marker, metadata change, missing object,
or deployment edit therefore stales the plan.

For each planned candidate, apply issues one version-specific delete with the
recorded `VersionId` and current-version ETag precondition. It never issues a
key-only delete or a multi-object delete. A failed precondition leaves that
object unchanged and stops the run.

After every delete response or exception, apply performs an exact-version read:

- absence proves that version was deleted;
- presence proves it remains and permits a later fresh preview;
- an unreadable result is `ambiguous`.

The tool performs no automatic retry. It stops after the first refused,
failed, or ambiguous candidate. Earlier proved deletions remain complete and
are listed separately from untouched candidates.

After all planned deletes, apply repeats the full inventory and verifies that
the deleted versions are absent and every retained or protected version is
present. Only that result is `applied`. A failed final inventory is
`applied_unverified`, and the operator must inspect status before another
apply.

The plan and result contain package identities, timestamps, sizes, policy
inputs, and bounded provider classifications. They exclude package bytes,
credentials, account secrets, raw provider responses, and presigned URLs.

### Authority

Terraform defines a separate operator-assumable
application-artifact-retirement role. Its bucket permission is limited to
version enumeration under the exact application-artifact prefix. Its object
permissions are limited to reading metadata and exact versions plus permanent
deletion of exact versions in that prefix.

The release-publisher role keeps its current create/read permissions and gains
no delete permission. Runtime roles gain no retirement permission.

Running publication, rollout, rollback, restoration, and retirement at the
same time is an operator error. A changed inventory stales an apply plan. The
runbook requires the operator to finish or abandon rollout and rollback work
before creating the retirement preview.

## Options considered

### A. Separate role and preview-bound exact-version deletion

Selected. It keeps deletion authority away from publication, proves both
floors from one complete inventory, and makes each irreversible action
traceable to a reviewed plan.

### B. Add deletion to the release-publisher role

Rejected. It uses fewer IAM resources, but it turns an append-only publisher
into a permanent-delete principal and weakens the current permission boundary.

### C. Use S3 lifecycle expiration or tags

Rejected. Lifecycle cannot express the newest-10 count. Tags would add mutable
retirement state and still need a trusted process to prove the count,
protected references, and conflict behavior before tagging.

### D. Keep packages forever

This is the current safe behavior and remains the fallback when inventory or
authority proof fails. It leaves the accepted retirement milestone incomplete
and continues storage accumulation.

## Consequences

Positive consequences:

- Every deleted version has retained plan evidence for both policy floors.
- Active, rollout, and rollback packages have an explicit protection seam.
- Publication remains append-only under its own role.
- A partial run never looks complete.
- A newly published or otherwise changed inventory invalidates an old plan.

Costs and limits:

- Operators must supply and verify the protected package set.
- The repository gains one operator role with permanent-delete authority.
- Ties at the newest-package boundary retain more than the configured floor.
- An unexpected object or version under the prefix blocks retirement until it
  is investigated through separate authority.
- A permanent deletion has no in-place rollback. Restoration needs approved
  exact bytes and creates a new S3 version.
- This tool does not decide whether old unresolved delivery evidence should be
  closed. ADR-020 leaves that evidence unchanged when its package is absent.

## Failure semantics

- Preview read or pagination failure: `inventory_failed`; no plan and no write.
- Inventory limit reached: `inventory_limit_exceeded`; no plan and no write.
- Malformed or conflicting inventory: `inventory_refused`; no plan and no
  write.
- Apply plan or inventory mismatch: `stale_plan`; no write.
- Conditional delete refusal: `delete_refused`; prior proved deletes remain
  recorded, later candidates remain untouched.
- Provider failure followed by proved presence: `delete_failed`.
- Provider failure followed by proved absence: deletion is recorded as proved.
- Unreadable exact-version status: `ambiguous`; no retry.
- Failed final inventory after proved deletes: `applied_unverified`.
- Complete exact verification: `applied`.

Every state except `applied` exits nonzero. Re-running apply with the old plan
is refused after any proved deletion because the inventory no longer matches.

## Verification

- Bind key grammar, metadata, version history, protected references, and
  complete pagination with unit tests.
- Test age immediately before, at, and after the cutoff.
- Test fewer than 10 packages, exactly 10, more than 10, and timestamp ties at
  the floor boundary.
- Prove preview writes nothing and canonical plan bytes have a stable digest.
- Prove a new object, removed object, changed ETag/version, delete marker, and
  changed protected set each stale apply before deletion.
- Prove every delete carries the exact `VersionId` and ETag precondition and
  never uses key-only or batch deletion.
- Test applied, refused, failed, ambiguous, partial, and final-verification
  outcomes without leaking provider text.
- Bind the separate role to its exact prefix and prove the publisher and
  runtime roles lack deletion.
- Run focused tests, Terraform validation, documentation and reference checks,
  `make check`, `git diff --check`, complete diff inspection, and hygiene
  checks.

No live package deletion belongs to implementation verification. A later
operator exercise needs separate authority, named disposable objects, and an
approved AWS identity.

## Migration and rollback

Applying the infrastructure change creates the retirement role and deletes no
package. Existing packages remain valid inventory when they satisfy the record
contract above.

Tool rollback removes future retirement access after any active run is settled.
It cannot restore a deleted version. Restore one package by obtaining its
approved exact ZIP bytes, recomputing the digest, publishing it through the
existing conditional publisher, and recording the new S3 `VersionId` before a
rollout or rollback uses it.

## Revisit conditions

Revisit this decision when:

- measured package count or size makes full bounded enumeration too costly;
- an approved deployment-state source can supply protected references without
  operator entry;
- normal operations produce version history or delete markers under package
  keys;
- concurrent retirement and rollout becomes a routine requirement;
- the 400-day or newest-10 floor changes;
- operators need archive restoration as a source-defined workflow.

Configuration-release retirement and source response-page retirement remain
separate decisions.

## References

- ADR-014: immutable release artifacts and retention
- ADR-020: exact application version and package replay window
- Specification 05: package storage, retention, and operator safety
- Specification 06: operations and package verification
- [Amazon S3 conditional deletes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-deletes.html)
- [Deleting object versions from a versioning-enabled bucket](https://docs.aws.amazon.com/AmazonS3/latest/userguide/DeletingObjectVersions.html)
- [Amazon S3 strong consistency](https://aws.amazon.com/s3/consistency/)

External references last checked: 2026-08-14.

References verified: 2026-08-14.
