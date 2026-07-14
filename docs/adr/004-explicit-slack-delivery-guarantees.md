# ADR-004: Explicit Slack delivery guarantees

- Status: Accepted
- Date: 2026-07-12

## Context

DynamoDB can prevent concurrent claims. It cannot atomically commit a Slack HTTP request and a delivery-state update. A timeout or worker termination can leave an unknown outcome.

## Decision

The service guarantees duplicate suppression for known outcomes. It does not claim exactly-once Slack posting.

Delivery records use these states:

- `pending_queue`: durable request exists and awaits queue dispatch.
- `queued`: dispatch to SQS succeeded.
- `sending`: a worker holds a lease and has an `attempt_id`.
- `posted`: Slack returned a documented success response.
- `failed_retryable`: Slack explicitly permits another attempt.
- `failed_terminal`: configuration or payload correction is required.
- `delivery_unknown`: Slack may have accepted the request.

Automatic retry is allowed for `failed_retryable`, and for transport failures only when the client proves that no request bytes were sent. A stale `sending` lease becomes `delivery_unknown`. Unknown outcomes require operator review. Manual replay records the operator, reason, prior attempt, and new attempt.

The SQS receive count and the Slack network-attempt counter are separate. Queue redelivery before a network call cannot exhaust Slack attempts. The worker stores Slack response metadata needed for diagnosis and, in bot mode, the returned message identifier when available.

## Consequences

- Known duplicates after `posted` are acknowledged and discarded.
- Ambiguous requests become visible work instead of silent replay.
- Tests must cover every transition and crash boundary.

## References

References verified: 2026-07-13.

- [Slack incoming webhooks](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/)
- [Slack rate limits](https://docs.slack.dev/apis/web-api/rate-limits/)
