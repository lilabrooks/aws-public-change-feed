# ADR-023: Scoped active-manifest absence detection

- Status: Accepted
- Date: 2026-08-21
- Owner: Lila Brooks
- Relates to: [ADR-019](019-s3-preconditions-for-release-publication-and-promotion.md)

## Context

ADR-019 gives a missing current `active-versions.json` object a specific job:
it selects the first-promotion path. The publisher reads that object before it
chooses `If-None-Match: *` or `If-Match`.

The deployed release-publisher role can read the current pointer and exact
release versions. It has no `s3:ListBucket` grant. A live read under that role
returned `403 AccessDenied` while the pointer was absent. AWS documents this
response rule for `GetObject`: an absent key returns 404 when the caller has
`s3:ListBucket` and 403 when it does not.

The adapter preserves 403 because it may describe a real object-level denial.
The publisher converts only `ObjectMissing` into first-promotion state. These
two correct local rules leave a new deployment unable to reach its first
promotion under the deployed role.

The feed watcher uses the same S3 adapter to read the current pointer. Its role
has the same absence ambiguity. Exact-version reads by either runtime have
separate semantics and permissions.

## Decision drivers

- Preserve a real object-level denial as a refusal.
- Reveal the smallest possible set of bucket key names.
- Keep the publisher and watcher adapter contract identical.
- Leave exact-version reads unchanged.
- Make failed or malformed provider results visible.
- Keep deployment proof separate from source proof.

## Decision

After `GetObject` returns HTTP 403 for an unversioned read, `S3ObjectStore`
will issue one `ListObjectsV2` request against the same bucket with:

- `Prefix` equal to the complete requested object key;
- `MaxKeys` equal to 1.

The adapter compares returned keys by exact string equality. An exact match
preserves the original `GetObject` error. A response with no exact match raises
`ObjectMissing`. A key sharing the requested prefix does not count as the
requested object.

A failed list request surfaces that provider error. A malformed list response
preserves the original `GetObject` refusal. The adapter retains no inferred
existence state, so every read begins with a fresh `GetObject`.

The fallback applies only to unversioned reads with HTTP status 403. Direct
404 translation, writes, other error statuses, and every read with a
`versionId` keep their existing behavior.

The release-publisher and feed-watcher policies each receive one
`s3:ListBucket` statement on the configuration bucket. Two conditions bound
the permitted request:

- `StringEquals` requires `s3:prefix` to equal the complete active-manifest
  key derived from `local.active_versions_key`;
- `NumericLessThanEquals` requires `s3:max-keys` to be at most 1.

The roles receive no version-list action through this decision.

## Failure semantics

One current-pointer read can end in these states:

- `GetObject` succeeds: return the stored object without listing.
- `GetObject` returns 404: raise `ObjectMissing` without listing.
- `GetObject` returns 403 and the probe finds no exact key: raise
  `ObjectMissing`.
- `GetObject` returns 403 and the probe finds the exact key: raise the original
  403.
- The exact probe fails: raise the probe's provider error.
- The exact probe is malformed: raise the original 403.

Publication stops on every refusal. Only `ObjectMissing` reaches the existing
first-promotion decision.

## Consequences

- A new deployment can express active-pointer absence under the same role that
  publishes its first release.
- An existing pointer that cannot be read stays an authorization failure.
- Each affected role can reveal at most one key whose name begins with the
  complete active-manifest key. The exact comparison prevents a suffix key
  from proving pointer existence.
- Source and policy tests can prove request shape and classification. They
  cannot prove deployed IAM or live S3 behavior.
- L-28 owns a complete central Terraform plan, review, apply, zero-drift check,
  and assumed-role first-pointer proof after this source change is accepted.

## Options considered

### Grant bucket-wide or directory-wide listing

This would let the original `GetObject` return 404 for absence. It exposes a
larger key-name set to publisher and runtime roles. The active-pointer decision
needs one complete key, so the wider grant was rejected.

### Create the first pointer manually

This removes the absent-key case from initial publication. It creates
out-of-band release state and leaves delete-marker recovery ambiguous. It was
rejected.

### Treat every current-object 403 as absence

This would let a real object denial enter the create path. It was rejected
because the classification could hide a broken object policy.

## Verification

- Assert the list request uses the same bucket, the complete key as `Prefix`,
  and `MaxKeys=1`.
- Cover empty, exact-key, prefix-sibling, failed, and malformed list results.
- Assert direct 404, versioned 403, and other status paths issue no list call.
- Invert the exact-key comparison and every IAM condition independently.
- Assert both role policies contain one bounded grant and no broader list
  action.
- Run the focused release, S3 precondition, and Terraform contract suites,
  followed by the repository aggregate gate.
- Under L-28, prove the deployed role reaches first-promotion preview when the
  current pointer is absent and preserves a real denial as a refusal.

## Migration and rollback

L-27 ships the adapter, both Terraform policy documents, governing text, and
local proof in one candidate. L-28 deploys and verifies the policy change.

Rollback deploys the prior application package and removes the 2 bounded list
statements through a reviewed Terraform plan. First promotion then returns to
the prior 403 blocker until another accepted absence mechanism exists. Existing
release objects and pointer versions require no data migration.

## Revisit conditions

Reconsider this decision if S3 adds a current-object existence operation with
an object-scoped permission, if policy evaluation cannot enforce both request
conditions, if live L-28 evidence disagrees with the documented 403/404 rule,
or if another current-pointer consumer cannot use the same failure contract.

## References

References verified: 2026-08-21.

- [S3 GetObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html)
- [S3 policy condition keys](https://docs.aws.amazon.com/AmazonS3/latest/userguide/amazon-s3-policy-keys.html)
- [Required permissions for S3 API operations](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-with-s3-policy-actions.html)
