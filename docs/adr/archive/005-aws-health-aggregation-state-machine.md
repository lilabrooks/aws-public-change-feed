# ADR-005: AWS Health aggregation state machine

- Status: Superseded by ADR-017
- Date: 2026-07-12

## Context

AWS Health events can arrive more than once, through a backup Region, across several affected accounts, and in several pages when the affected-resource list is large. The original specification did not define group identity, window movement, flusher ownership, or late updates.

## Decision

### Identity

Use `detail.affectedAccount` as the affected account in organization feeds. Use the source account only when `affectedAccount` is absent in a validated account-specific payload.

The normalized event identity is:

```text
sha256(eventArn + "#" + affectedAccount + "#" + communicationId)
```

Backup copies with the same identity are ignored through a conditional event-detail write.

The aggregation key is:

```text
health-group#{sha256(eventArn)}#{normalized_service}#{risk_type}#route#{route_id}#g#{generation}
```

The event-detail sort key is:

```text
event#{affectedAccount}#{sha256(communicationId)}
```

Generation starts at `1`. A material event update received after a posted generation creates the next generation through a conditional transaction. Material updates include a newly affected environment or resource, a changed start time, or increased actionability. Closed-status alerts are controlled by `health_alert_on_closed`, which defaults to `false`.

### Window

The first unique event sets:

```text
first_seen_at = now
flush_after = now + window_minutes
flush_deadline = now + max_window_minutes
```

Each later unique event moves `flush_after` to the earlier of `now + window_minutes` and `flush_deadline`. Duplicate identities do not move the window.

Store `page` and `totalPages` for EventBridge messages. The group waits for all known pages until `flush_deadline`. At the deadline, an incomplete group posts with an incomplete-resource warning and emits an operational metric.

### State and concurrency

Aggregation metadata uses these states:

- `pending`
- `claimed`
- `delivery_pending`
- `posted`
- `delivery_unknown`
- `failed_terminal`

The collector uses a DynamoDB transaction to conditionally insert the event detail and update group metadata. The flusher queries due `pending` groups, conditionally changes one group to `claimed`, and sets a claim lease. It then performs a strongly consistent query of the base-table partition before building the message.

After a successful queue send, the flusher moves the aggregation to `delivery_pending` and stores the route-scoped alert key. The worker from ADR-007 owns the delivery record and outcome rules from ADR-004. The worker moves the aggregation to `posted`, `delivery_unknown`, or `failed_terminal` from the matching delivery outcome.

An expired `claimed` lease can be reclaimed before Slack sending begins. An expired Slack `sending` lease follows ADR-004 and becomes `delivery_unknown`.

All metadata and detail records receive the configured aggregation TTL. Late material updates after `posted` use a new generation and produce a message labelled as an update.

## Consequences

- Backup events and paginated resources can be combined without duplicate route messages.
- Sliding windows have a fixed upper bound.
- A concurrent flusher cannot own the same generation after a successful conditional claim.
- The Health collector role needs `dynamodb:TransactWriteItems` in addition to item and query permissions.

## References

References verified: 2026-07-12.

- AWS Health EventBridge schema: https://docs.aws.amazon.com/health/latest/ug/aws-health-events-eventbridge-schema.html
- AWS Health EventBridge pagination: https://docs.aws.amazon.com/health/latest/ug/pagnation-of-health-events.html
- AWS Health Region and backup-event dedupe guidance: https://docs.aws.amazon.com/health/latest/ug/choosing-a-region.html
- Central delivery queue decision: ../007-central-slack-delivery-queue-and-worker.md
