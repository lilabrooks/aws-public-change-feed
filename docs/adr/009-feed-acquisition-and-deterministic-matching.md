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

## Revision: acquisition owns the stored-URL boundary

- Status: Accepted
- Date: 2026-08-10
- Accepted: 2026-08-10

The producer must reject a raw item URL before persistence when its syntax is
malformed, it carries user information, it contains an unencoded character
outside the URI character set, or it has a malformed percent escape. Scheme
and hostname case, an explicit default port, and a fragment are canonicalized
as chapter 04 specifies. The original accepted URL remains in provenance while
its canonical form supplies announcement identity.

The producer and every later candidate consumer apply one reachable-state
rule: any URL accepted into a candidate must pass the worker's source-link
policy after canonicalization. A regression matrix covers the raw forms at the
producer, the canonical URL stored on the announcement, and the exact
provenance URL checked during delivery.

A feed publication time remains optional. When present on a candidate, it must
be a parseable normalized timestamp. No ordering against `observed_at` is
inferred because the public feed's clock may differ from the watcher clock and
the product has no accepted skew policy.

## References

References verified: 2026-07-13.

- [AWS News Blog feeds](https://aws.amazon.com/blogs/aws/feed/)
- [AWS What's New RSS feed](https://aws.amazon.com/about-aws/whats-new/recent/feed/)
- [AWS Security Bulletins](https://aws.amazon.com/security/security-bulletins/rss/feed/)
- [OWASP server-side request forgery prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
