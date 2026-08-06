# ADR-019: S3 preconditions for release publication and promotion

- Status: Accepted
- Date: 2026-08-04
- Amends: [ADR-014](014-immutable-release-artifacts-and-retention.md)

## Context

ADR-014 promotes the active pointer "with a compare-and-swap condition against the previously observed S3 version", and chapter 03 repeats it as "only if its prior S3 version matches the publisher's observed version". S3 offers no such precondition. Conditional writes accept `If-None-Match`, which guards on the absence of a current object at the key, or `If-Match`, which S3 evaluates by comparing the supplied value against the object's ETag. A version ID identifies an exact stored version for reads and cannot appear in a write precondition at all.

The error is confined to the write condition. Every other use of version IDs in ADR-014 is correct: the manifest pins the exact `config.yaml` and `inventory.json` versions, and the worker loads those versions before rendering. Reads by version ID are precisely what S3 supports.

No S3 adapter exists yet. This corrects the contract before the milestone-2 publisher is written against it, rather than discovering it when a promotion test cannot be expressed.

Review of the first draft surfaced a second defect, older than the precondition error. "Manifest" names two different things across the accepted documents. ADR-001, ADR-011, chapter 03's active-manifest section, chapter 05's IAM roles, and the runbook's rollback procedure all use it for `active-versions.json`, the versioned pointer. ADR-014 and chapter 03's publication sequence instead imply a *separate* immutable manifest object written into the release prefix. That second object has no schema, no key convention, and no consumer anywhere in the repository. A publisher cannot be written against it.

## Decision

Amend ADR-014 on two points. Publication and promotion use HTTP preconditions, and the two identifiers carry separate jobs: the ETag is the concurrency token, the version ID is the audit and read reference. The separate immutable manifest object is dropped.

### The manifest is the pointer

`active-versions.json` is the release manifest. It already carries everything ADR-014 asks a manifest to carry — release ID, object keys, version IDs, hashes, and schema versions — and S3 versioning already makes each promotion a retained, immutable record. Every prior version is a historical manifest, which is what the runbook's "identify the active manifest S3 version" and "promote the retained prior manifest" already assume.

So publication writes two immutable objects, not three, and the pointer write *is* the promotion. Nothing is lost: retention still applies to pointer versions, and rollback still reads an exact earlier version by ID.

### Immutable object creation

Write `config.yaml` and `inventory.json` with `If-None-Match: *`. Publish them as single-part uploads.

- `412 Precondition Failed` means an object already exists at that key. Release keys embed the content-derived release ID, so an existing object is expected to carry identical bytes. Read it back and compare its SHA-256 against the computed hash. Adopt the existing version if the hash matches; fail publication if it does not, because that is an out-of-band write rather than a concurrency event.
- `409 Conflict` means a delete raced the write. Retry the `PutObject` a bounded number of times, then fail. Single-part uploads keep this retry in place; `CompleteMultipartUpload` would require re-initiating the whole upload.
- `If-None-Match` evaluates only the current version. On a versioned bucket a delete marker makes a key look absent, so a create can succeed at a key that still holds noncurrent versions. This is harmless here because the pointer pins the version ID the publisher read back and every consumer verifies the hash.

### Active-pointer promotion

Read the current `active-versions.json` and capture both its ETag and its version ID from that read. Write the new pointer with `If-Match` set to the observed ETag, then capture the new version ID from the response.

The observed ETag must come from the same read that produced the state the publisher decided against. A precondition read taken separately from the decision is not a compare-and-swap.

Outcomes:

- `200 OK`: promoted. Record the prior and new version IDs alongside the prior and new release IDs.
- `412 Precondition Failed`: another publisher promoted between the read and the write. Do not retry automatically. Promotion chooses which release is active, so a blind retry replaces a competing publisher's decision with a stale one. Re-read, re-validate, and require a fresh decision; in CI, fail the job and report both release IDs.
- `409 Conflict`: a concurrent request left the outcome indeterminate. Re-read the pointer. If it names this release, promotion has converged and the publisher records the desired release as active. It does not record that this request succeeded: a competing publisher promoting the same release produces an identical pointer, and S3 does not attribute the winning write. Record convergence, not attribution. If the pointer names anything else, treat it as not promoted and restart from the read.
- `404 Not Found`: there is no current version, or the current version is a delete marker. `If-Match` returns 404 in this case rather than 412. A first promotion into a new deployment is expected to hit it and uses `If-None-Match: *` instead; a 412 there means the pointer already exists, so the publisher restarts on the `If-Match` path. A 404 against a pointer expected to exist stops publication and raises an operational alarm. Never fall back from `If-Match` to `If-None-Match` automatically, which would turn a deleted pointer into a silent re-creation.

### Rollback promotion

Rollback promotes an earlier retained pointer version through the same `If-Match` path against the currently observed ETag. It reads the historical version by ID, then writes its release references forward as a **new** pointer document carrying a fresh `promoted_at`. It never deletes pointer versions, rewrites release objects, or reuses a prior version ID.

The fresh `promoted_at` is load-bearing, not bookkeeping. Republishing identical historical bytes would reproduce the historical ETag, so a concurrent publisher holding that old ETag would find its precondition satisfied against a pointer that had moved away and come back. `promoted_at` is already required by `active-versions.schema.json`, so a rollback that records its own promotion time cannot collide with the version it restores.

### Integrity stays separate from concurrency

The ETag is used only as an opaque concurrency token. Content integrity remains the manifest's SHA-256 hashes, verified by the publisher on read-back and by the runtime before rendering. S3 does not define the ETag as a content hash across all upload and encryption modes, and identical content can produce an identical ETag on two versions of a key. No code may treat an ETag as a content hash or as a version identifier.

## Consequences

- IAM needs two distinct read actions, and granting only the obvious one breaks read-back. `s3:GetObject` covers reads of the *current* object, which is what `If-Match` promotion requires on the pointer key. Any read that supplies a `versionId` requires `s3:GetObjectVersion` instead — AWS states that `s3:GetObject` "is not required in this scenario". So the publisher needs `s3:GetObject` and `s3:PutObject` on the pointer key, plus `s3:GetObjectVersion` for the exact-version read-back in step 5 and for reading a historical pointer version during rollback. The feed watcher needs `s3:GetObject` on the pointer and `s3:GetObjectVersion` on release objects, because it loads exact versions. Chapter 05 describes both roles as reading exact versions, so their described scope is unchanged, but the Terraform policies must name both actions.
- Conditional writes require Signature Version 4. Default boto3 configuration satisfies this; a future endpoint or signing override must not silently drop the precondition.
- Concurrent publishers remain safe, and the failure surface is now explicit. 412, 409, and 404 carry different meanings and different responses, so a single "compare-and-swap failed" branch is no longer sufficient.
- Version IDs stay in the pointer, in candidates, and in the runbook's rollback procedure, unchanged.
- Publication writes two immutable objects rather than three. No schema, example, or validator changes, because the dropped manifest never had one — that absence is what identified it as a phantom. Chapter 03's publication sequence loses a step and ADR-014's "write an immutable manifest" clause is superseded.
- The milestone-2 acceptance test for compare-and-swap promotion can now be written against a real header contract instead of an invented one.

## Milestone-2 testing decision

- Status: Proposed

The open question this ADR left for milestone 2 was boto3 against moto, or an opt-in integration suite against a dedicated real bucket. It named three places where mock fidelity was unproven: concurrent-publisher interleaving, the indeterminate `409` outcome, and versioned-bucket behavior including delete markers and the `404` case. All three were measured against moto 5.2.2 and boto3 1.43.65 before this section was written. `tests/test_s3_preconditions.py` carries the checks.

**Use moto for single-request precondition semantics.** Every clause above that a single request can express behaves as this ADR specifies, including the two that were doubted: `If-Match` against a delete marker and against a never-written key both return `404 NoSuchKey` rather than `412`, and `If-None-Match: *` succeeds over a delete marker while noncurrent versions remain. Exact-version read-back returns the original bytes, and identical content reproduces the prior ETag, which is the hazard that makes rollback's fresh `promoted_at` load-bearing.

**Do not use moto to verify concurrent promotion.** Moto does not evaluate a conditional write atomically. Twelve publishers released together against one shared ETag produced two winners in 13 of 60 trials, so the compare-and-swap property is not enforced under the mock. A promotion test asserting a single winner would pass roughly four times in five and would be worse than no test, because a green run would read as proof of the exact property that is absent. Concurrency was measured rather than assumed precisely because a single trial had shown one winner and looked conclusive.

**Test both `409` branches by injection.** No deliberate create/delete race produced `409 ConditionalRequestConflict` under moto. A real bucket does not fix this: `409` is a genuine race that cannot be provoked on demand there either. The bounded retry on creation and the convergence-without-attribution handling on promotion are publisher logic, so they are tested by raising a `ClientError` carrying that code at the publisher's S3 seam. That measures the response to the outcome, which is the part carrying the decision.

**No real-bucket suite gates milestone 2.** The properties a real bucket would add over moto are concurrent serialization and `409`, and neither is reliably reproducible in a test. Introducing credentials, a dedicated bucket, and a scoped role to gain an unreliable signal is not proportionate. Reconsider if the publisher later depends on behavior this list does not cover.

Two limits of this evidence. Moto agreeing with this ADR shows the mock and the ADR share a reading of S3, not that the reading is correct; the two sources are only partly independent, since this ADR cites the AWS documentation and moto carries AWS-verified precondition tests. And the atomicity result is a property of moto 5.2.2 that an upstream fix could change, which is why the committed test asserts the sequential behavior the publisher may rely on rather than asserting the defect.

## Rollback

If S3 later offers a version-ID write precondition, revisit the promotion clause and prefer it, since it removes the split between the concurrency token and the audit reference. If stopping publication on 412 proves disruptive in a pipeline with frequent concurrent promotions, serialize promotion in CI rather than retrying the overwrite; automatic retry is the behavior this decision exists to forbid.

## References

References verified: 2026-08-04.

- [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
- [S3 versioning](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html)
- [S3 GetObject, including the `versionId` permission distinction](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html)
- [Checking object integrity in S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity.html)
