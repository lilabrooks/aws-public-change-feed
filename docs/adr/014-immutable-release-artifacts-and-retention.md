# ADR-014: Immutable release artifacts and retention

- Status: Accepted, amended
- Date: 2026-07-12
- Amended by: [ADR-019](019-s3-preconditions-for-release-publication-and-promotion.md) on 2026-08-04. Two clauses in the decision below no longer govern. Everything else here stands.

## Context

Candidates and delivery requests must remain reproducible across retries and manual review. Mutable S3 keys can pair policy from one update with inventory from another.

## Decision

Publish `config.yaml` and `inventory.json` under a new write-once release prefix. Compute their SHA-256 hashes and a release ID from the ordered manifest fields. Write an immutable manifest, then promote the active pointer with a compare-and-swap condition against the previously observed S3 version.

> **Amended by ADR-019.** The last sentence above is preserved as the original record and no longer governs. There is no separate immutable manifest object: `active-versions.json` is the manifest, and its versions are the retained history. Promotion compares the pointer's **ETag** with `If-Match`, because S3 has no version-ID write precondition. Version IDs remain correct everywhere else in this decision, including the paragraph below.

Every candidate embeds the release ID, object keys, version IDs, hashes, schema versions, and application version. The worker loads those exact object versions and verifies hashes before rendering.

Retain release objects for at least the longest period in which a delivery record can be retried, investigated, or manually replayed. The canonical deployment keeps releases for 400 days and delivery state for 365 days. S3 versioning is enabled; lifecycle expiration must preserve that invariant.

## Consequences

- Replays use the policy and inventory that produced the candidate.
- Concurrent promotions cannot silently overwrite each other.
- Retention settings are validated across deployment and runtime documents.

## References

References verified: 2026-07-13.

- [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
- [S3 versioning](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html)
- [S3 lifecycle management](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html)
