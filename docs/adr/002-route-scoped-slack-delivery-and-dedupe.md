# ADR-002: Route-scoped candidates and delivery identity

- Status: Accepted
- Date: 2026-07-12

## Context

One public announcement can match several profiles and customer routes. Slack delivery, replay, and dedupe need a stable unit that cannot leak one route's environment data into another route.

## Decision

Create one `AlertCandidate` per announcement revision, service, risk type, route, and matching audience. The candidate contains sorted environment IDs for that route and labels them as potentially relevant. The worker resolves display details from the candidate's exact inventory release.

Identity uses SHA-256 over UTF-8, null-framed fields. Identity inputs containing a null character are invalid:

- `announcement_id = SHA256(canonical_url)`
- `content_fingerprint = SHA256("announcement-content:v1", normalized_title, normalized_summary)`
- `revision_id = SHA256("announcement-revision:v1", announcement_id, content_fingerprint)`
- `audience_fingerprint = SHA256("candidate-audience:v1", sorted_environment_ids...)`
- `candidate_id = SHA256("candidate:v3", revision_id, service_id, risk_type, route_id, audience_fingerprint)`
- `request_id = SHA256("delivery-request:v2", candidate_id)`

The hash-domain versions describe logical identity and are independent of the JSON envelope contract version. Adding release metadata without changing revision, service, risk type, route, or audience therefore does not create new logical work.

Canonical URL processing is limited to documented normalization that preserves resource identity. Feed name is provenance. Matching rule IDs remain explainability metadata and do not affect identity. An overlapping feed therefore enriches provenance without creating a second candidate. A changed route audience creates a new candidate.

The grouping unit is one candidate: one announcement revision, service, risk type, route, and the complete matching environment set. Cross-candidate batching is outside the current delivery contract.

The delivery record is keyed by `candidate_id`. A posted record suppresses duplicate automatic delivery. A new normalized title or summary creates a revision and may produce a new candidate. Provenance-only additions do not.

## Consequences

- Delivery isolation follows the route boundary.
- Rule refactors do not resend unchanged content.
- Material source edits can be reviewed as new work.
- Replays retain the exact release and candidate that originally produced the request.
