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

## Revision: capacity is a release invariant and pacing starts at completion

- Status: Accepted
- Date: 2026-08-10
- Accepted: 2026-08-10

Configuration publication rejects a message policy whose maximum title,
summary, explanation, and recommended-action fields cannot fit the required
message structure within `max_message_characters`. The calculation includes
both the top-level fallback and Block Kit text, fixed labels and link text, and
an environment summary that visibly reports omitted entries. The renderer owns
the calculation, and a test renders the same worst-case values to catch drift.

The canonical release therefore reduces `max_summary_characters` from 1,200 to
300. Its binding case is the 1,047-character source URL allowed by the shared
512-character path and 512-character query limits. The URL appears in both the
source link and the fallback, consuming 2,094 of the 4,000-character aggregate
budget. In the 44-item labeled corpus, 27 summaries exceed 300 characters and
14 exceed the former 1,200-character cap; the normalized median is 1,047 and
the maximum is 1,770. This decision favors guaranteed rendering for every URL
the release accepts and a short Slack excerpt. The complete normalized summary
remains in the durable candidate, and Slack remains a delivery surface rather
than the source of truth. A separate source-item URL cap or a different message
budget would be a later product decision with its own acquisition and rendering
evidence.

### Production-preflight disposition: retain the 300-character cap

The 2026-08-14 owner review retains the 300-character Slack summary cap for
production preflight. The renderer-built Slack sample remains separate L-10
evidence and does not reopen this limit by itself. The current baseline is that
28 of 45 labeled corpus summaries exceed 300 characters, compared with 15 at
the former 1,200-character cap; the normalized median is 1,056 and the maximum
is 1,770.

Candidate changes include a distinct source-item URL bound or revised aggregate
message-budget semantics. Either would alter the acquisition or rendering
contract, so it requires its own chapter 04 and ADR decision rather than an
implicit production-preflight adjustment.

The environment list remains the data-sized field that yields at render time.
It is shortened deterministically, keeps an omitted count, and is charged for
its exact label in both copies. The final whole-message measurement remains a
refusal backstop for stored releases that predate the publication rule.

Worker time is read at each state boundary. Due-work checks use the invocation
observation, the sending lease starts immediately before the conditional
claim, and retry scheduling, destination pacing, and resolved-state TTL use a
fresh time after the Slack call returns or raises. A slow credential read or
network call therefore cannot consume part of a future lease or retry delay.

## References

References verified: 2026-07-13.

- [Slack Block Kit composition objects](https://docs.slack.dev/reference/block-kit/composition-objects/text-object/)
- [Slack message formatting](https://docs.slack.dev/messaging/formatting-message-text/)
- [Slack rate limits](https://docs.slack.dev/apis/web-api/rate-limits/)
- [Slack incoming webhooks](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/)
