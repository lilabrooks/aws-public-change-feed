# ADR-003: EventBridge-first AWS Health ingestion

- Status: Superseded by ADR-017
- Date: 2026-07-12

## Context

AWS Health sends direct service events to EventBridge with durable, at-least-once delivery. AWS Health API access requires a qualifying AWS Support plan, and Health event coverage depends on Region rules. One account switch also mixed Health ingestion with Cost Explorer access even though the services have different trust and endpoint requirements.

## Decision

Split the deployment choice into `health_access_mode` and `cost_access_mode`.

ADR-012 determines whether this repository owns either capability. The modes below apply when it does. An augmented deployment reuses an existing organization Health feed rather than creating a duplicate ingress path.

Supported Health modes:

- `organizations_eventbridge`: enable or verify AWS Health organizational view, then receive the organization feed in the management or delegated administrator account.
- `cross_account_eventbridge`: deploy an `aws.health` EventBridge rule in each approved account and forward raw events to the central event bus. Source accounts need no collector Lambda. The central `aws-health-event-collector` normalizes every forwarded event before aggregation.
- `health_api_polling`: assume approved account roles and poll AWS Health APIs. This is an exception mode and requires deployment validation that the required AWS Support plan and Health endpoints are available.

Supported cost modes:

- `management_account`: query Cost Explorer centrally and filter or group by linked account.
- `cross_account_roles`: assume a dedicated cost role in each approved account.
- `disabled`: omit the cost lane.

For the standard AWS partition, the default Health Region strategy is `simplified`:

- Create the account-specific Health rule in `us-west-2` to receive account-specific events from standard-partition Regions.
- Create a rule in `us-east-1` for global Health events.
- Forward both rules to the central ingress bus in the deployment Region.
- Filter the Health lane to `ACCOUNT_SPECIFIC`; public change awareness remains in the RSS lane.

An optional `high_availability` strategy creates rules in each configured workload Region and its AWS-documented backup Region. Backup copies are deduplicated using `eventArn`, `communicationId`, and `affectedAccount`.

The deployment Region does not set every AWS client endpoint. Health API polling uses supported Health endpoints. Cost Explorer uses `ce.us-east-1.amazonaws.com`.

## Consequences

- Most deployments receive Health events without scheduled API polling.
- API polling remains available for customers that cannot forward EventBridge events.
- Cross-account deployment must create narrow event-bus resource policies and rule target roles.
- Partition-specific Region behavior requires a deployment validation rule before apply.

## References

References verified: 2026-07-12.

- AWS Health EventBridge monitoring: https://docs.aws.amazon.com/health/latest/ug/cloudwatch-events-health.html
- AWS Health organization aggregation: https://docs.aws.amazon.com/health/latest/ug/aggregating-health-events.html
- AWS Health Region coverage: https://docs.aws.amazon.com/health/latest/ug/choosing-a-region.html
- AWS Health concepts and API Support-plan requirement: https://docs.aws.amazon.com/health/latest/ug/aws-health-concepts-and-terms.html
- EventBridge cross-account routing: https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-cross-account.html
- EventBridge cross-Region routing: https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-cross-region.html
- Cost Explorer API endpoint: https://docs.aws.amazon.com/cost-management/latest/userguide/ce-api.html
