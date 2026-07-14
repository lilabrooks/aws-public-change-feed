# ADR-010: Operations and supported scale

- Status: Accepted
- Date: 2026-07-12

## Context

Schedules and AWS managed services do not establish a usable capacity or support promise. Feed freshness and Slack destination limits need measurable bounds.

## Decision

The initial supported envelope is:

- 100 AWS accounts represented as inventory metadata.
- 500 environments.
- 25 public feeds.
- 20 Slack routes and unique destinations.
- 100 service definitions, 100 profiles, and 50 risk rules.
- 300 generated deliveries per hour globally and per destination in the canonical deployment.

Schema limits may allow up to 1,000 deliveries per hour for a tested deployment. The declared global rate cannot exceed the sum of route limits or the worker's measured pacing capacity. Each deployment records a load-test artifact for its actual Lambda concurrency, request duration, queue visibility, DynamoDB capacity mode, and Slack pacing.

Emit per-feed fetch success, fetch age, newest observed publication age, parse rejection counts, normalized announcements, matched candidates, outbox age, queue age, delivery states, Slack response classes, DLQ depth, and reconciler repairs. Alarm independently on feed staleness, dispatcher backlog, queue backlog, unknown outcomes, terminal failures, and missing scheduler or worker heartbeats.

## Consequences

- Operators can distinguish a quiet feed from a failed fetch path.
- Capacity changes require evidence and configuration review.
- The supported envelope is conservative enough for destination-paced delivery of ready work.

## References

References verified: 2026-07-13.

- [CloudWatch alarms](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/AlarmThatSendsEmail.html)
- [SQS CloudWatch metrics](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-available-cloudwatch-metrics.html)
- [Lambda scaling with SQS](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-scaling.html)
