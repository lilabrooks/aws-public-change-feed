# ADR-017: Public-feed-only product scope

- Status: Accepted
- Date: 2026-07-13

## Context

The earlier design combined public AWS announcement review, AWS Health processing, cost comparison, optional external-platform handoff, and Slack delivery. The public-feed lane had the clearest product purpose. The other lanes added separate evidence models, account access, state machines, and AWS-native capability overlap.

The product owner selected public AWS announcement intelligence as the complete product and Slack as its delivery channel.

## Decision

The service performs these jobs:

1. Fetch approved AWS RSS or Atom feeds.
2. Normalize and coalesce announcements across feeds.
3. Match each announcement against configured service definitions and risk rules.
4. Map inferred relevance through service profiles to configured customer environments.
5. Create one route-scoped, explainable candidate for each announcement revision, service, risk type, and Slack route.
6. Persist the candidate in a durable outbox.
7. Deliver grouped candidate details to Slack with destination pacing, retries, dedupe, and explicit unknown outcomes.

AWS Health ingestion, AWS Organizations integration, cost collection, Cost Explorer access, security-finding ingestion, customer-account roles, and external delivery adapters are outside this repository.

Public announcements provide review evidence. They do not confirm account, environment, or resource impact. Slack is the delivery channel. It is not an incident or remediation system of record.

The runtime has no customer-account permissions. Account IDs, Regions, customer labels, environment IDs, service profiles, and route assignments are static mapping inputs used to explain potential relevance.

This decision supersedes the former AWS Health, cost, and external-platform ADRs. [Archived copies](archive/README.md) retain those decisions for audit outside the current contract.

## Consequences

- The service has one source class, one evidence label, and one delivery path.
- Terraform needs only bootstrap and central roots.
- Candidate and delivery contracts cover public announcements only.
- Configuration has no Health, cost, cross-account role, or adapter fields.
- Teams needing confirmed impact continue to use their existing AWS Health or security tooling separately.

## References

References verified: 2026-07-13.

- AWS What's New RSS notice: https://aws.amazon.com/blogs/aws/subscribe-to-aws-daily-feature-updates-via-amazon-sns/
- AWS Security Bulletins: https://aws.amazon.com/security/security-bulletins/
- Slack incoming webhooks: https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/
