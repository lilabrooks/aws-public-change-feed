# ADR-015: Slack rendering, rate control, and retry

- Status: Accepted
- Date: 2026-07-12

## Context

Several routes may share a Slack destination. Slack accepts untrusted public text, applies destination-specific limits, and can return retry instructions longer than a Lambda invocation.

## Decision

`destination_key` is the ready-delivery ordering and pacing boundary. It is unique for each Slack destination even if route labels differ. The FIFO queue uses it as the message group. ADR-007 defines how a future-scheduled retry can follow newer ready work.

The worker claims a delivery only when the destination's `next_allowed_at` permits it. Otherwise it leaves the record retryable with a future `next_action_at`. After a request, it advances destination pacing using the configured minimum interval and a valid bounded `Retry-After` value. Long delays return to the durable queue and outbox instead of sleeping in Lambda.

Render source titles, summaries, customer labels, and recommendations as Slack `plain_text` where possible. Escape `&`, `<`, and `>` in the top-level fallback text. Include source URL, publication or observation time, service, risk, explanation, potentially relevant environments, and recommended review action. High-priority mention behavior uses a configured user-group ID, never a source-derived string.

Incoming-webhook runtime validation requires HTTPS, port 443, an approved hostname, the expected Slack webhook path, and no redirects. Bot mode derives the configured destination key from lowercased workspace and channel IDs. Secret values never enter contracts or logs.

Retry only documented retryable status classes. Bound `Retry-After`, exponential backoff, network attempts, SQS receives, and message size. Follow ADR-004 for ambiguous outcomes.

## Consequences

- Shared destinations cannot bypass pacing through multiple route IDs.
- Public content cannot inject Slack formatting or mentions.
- Long retry windows survive worker termination.

## References

References verified: 2026-07-13.

- [Slack Block Kit composition objects](https://docs.slack.dev/reference/block-kit/composition-objects/text-object/)
- [Slack message formatting](https://docs.slack.dev/messaging/formatting-message-text/)
- [Slack rate limits](https://docs.slack.dev/apis/web-api/rate-limits/)
- [Slack incoming webhooks](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/)
