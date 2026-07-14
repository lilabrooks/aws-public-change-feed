# ADR-013: Feed state and announcement identity

- Status: Accepted
- Date: 2026-07-12

## Context

HTTP validators are source checkpoints, while announcements can overlap feeds and change after publication. Treating either feed position or GUID as global identity loses edits or creates duplicates.

## Decision

Store feed state and announcement state as separate DynamoDB item types.

Feed state contains URL, ETag, Last-Modified, last attempt, last success, last error, and newest observed publication time. Update validators only after every candidate and outbox record created by that response is durable.

Announcement state is keyed by `announcement_id`, derived from the canonical URL. It records the latest content fingerprint, known revision IDs, first and last observation, merged provenance, normalized title and summary, source timestamps, and immutable release references used for emitted candidates.

All feeds in one scheduled run are fetched before announcements are emitted. Coalescing merges provenance for identical canonical URLs. The candidate preserves provenance known at evaluation. A later provenance-only addition updates announcement state without another Slack delivery. A normalized title or summary edit creates a new revision and may emit new candidates.

Conditional writes protect feed checkpoints and announcement merges from overlapping invocations. Expired terminal history may be replaced only with a condition that proves the stored TTL has passed. Unresolved delivery states have no TTL or receive an extension until resolved.

## Consequences

- Feed retries cannot skip undurable work.
- Overlap and edits have deterministic behavior.
- Source history supports replay and audit without making raw feed responses permanent.

## References

References verified: 2026-07-13.

- [DynamoDB conditional writes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html)
- [DynamoDB transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transactions.html)
- [DynamoDB TTL](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html)
