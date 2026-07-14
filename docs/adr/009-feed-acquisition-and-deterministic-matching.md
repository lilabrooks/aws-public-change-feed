# ADR-009: Feed acquisition and deterministic matching

- Status: Accepted
- Date: 2026-07-12

## Context

Public feeds are untrusted network input. Feed overlap, edits, vague service names, and broad risk words can otherwise cause missed items, duplicate work, or false matches.

## Decision

Fetch only configured HTTPS URLs from approved hosts. Resolve DNS before each request, reject private or special-purpose addresses, connect only to the validated address, verify TLS for the approved host, disable redirects, cap compressed and decoded bytes, and bound parse time and item count. Store the last successful ETag and Last-Modified values per feed.

Fetch all enabled feeds for a run, normalize and coalesce announcements by canonical URL, merge provenance, then match. A feed failure does not erase another feed's evidence and raises a feed-specific freshness alarm.

Matching is deterministic:

1. Normalize title and summary with Unicode normalization, case folding, whitespace collapse, and punctuation-aware tokenization.
2. Identify a configured service using globally unique aliases. Reject overly generic aliases.
3. Evaluate risk rules expressed as `any`, `all`, and `none` terms.
4. Require service evidence and risk evidence from distinct spans. A positive risk term cannot equal a service alias.
5. Map the service to enabled profiles and route-scoped environment sets.

The dedicated AWS Security Bulletins feed is public guidance and follows the same inferred-relevance rules. Source text is never interpreted as confirmed customer exposure.

## Consequences

- Network and parser behavior is bounded.
- Overlapping feeds enrich one announcement record.
- Match results are reproducible from source content and release artifacts.
- Historical corpus tests are required before rule promotion.

## References

References verified: 2026-07-13.

- [AWS News Blog feeds](https://aws.amazon.com/blogs/aws/feed/)
- [AWS What's New RSS feed](https://aws.amazon.com/about-aws/whats-new/recent/feed/)
- [AWS Security Bulletins](https://aws.amazon.com/security/security-bulletins/rss/feed/)
- [OWASP server-side request forgery prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
