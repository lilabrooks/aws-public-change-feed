# ADR-008: Cost attribution and comparison rules

- Status: Superseded by ADR-017
- Date: 2026-07-12

## Context

The cost lane did not define zero baselines, estimated data, currency, pagination, shared accounts, tag attribution, negative values, or billing restatements.

## Decision

### Attribution

Every deployed environment has one `cost_scope`:

- `account`: the environment owns the whole account for alerting purposes.
- `tag`: filter by one active cost-allocation tag key and value.
- `disabled`: skip cost checks for the environment.

Only one environment per AWS account can use `account`. Accounts with several monitored environments must use one active allocation tag key and unique values, or disable ambiguous environments. Mixing tag keys in one account is rejected because the resulting resource sets can overlap.

In `management_account` mode, account scopes use one query grouped by `LINKED_ACCOUNT` and `SERVICE`. Tag scopes use a per-account query filtered by `LINKED_ACCOUNT` and grouped by `SERVICE` and the configured tag. In `cross_account_roles` mode, query each account and group by `SERVICE`, plus the configured tag for tag scopes.

### Periods and API handling

Use UTC dates and one contiguous daily query spanning both comparison periods:

```text
current_end = utc_today - data_lag_days
current_start = current_end - lookback_days
prior_end = current_start
prior_start = prior_end - comparison_days
query = [prior_start, current_end)
```

The baseline uses `data_lag_days: 2`. Follow every `NextPageToken` with unchanged request parameters. Cache the completed response for the run so one source period is queried once.

If any included result has `Estimated: true`, skip that scope, emit `CostDataEstimated`, and retry on the next schedule. Every metric value must have `Unit: USD`; another unit fails that scope and emits `CostUnitMismatch`.

Parse amounts as decimal values. Round only for display. Alert only on a positive delta.

When prior cost is at least `minimum_baseline_usd`, both the dollar and percentage thresholds must pass. When prior cost is below that baseline, percentage is undefined and `minimum_delta_usd` alone can produce risk type `new_spend`.

Negative prior values are treated as below-baseline. Negative or lower current values do not produce an increase alert.

### Restatements

Store exact period amounts, eligibility, and a snapshot hash in the ADR-013 source-state table. A later non-estimated response can post if that period has never produced an alert and now becomes eligible. A period that already posted does not post again; changed values update audit fields and emit `CostDataRestated`.

Before production planning, apply the Cost Explorer preflight in ADR-016. It confirms account access, billing view, allocation tags, role permissions, query shape, and the expected unit with a real `GetCostAndUsage` request.

## Consequences

- Shared accounts require explicit cost allocation.
- API pagination and estimated results cannot silently change calculations.
- New spend has defined behavior when percentage change has no useful denominator.
- Cost Explorer request cost is `request pages × current per-page price`; the run emits page counts for cost review.

## References

References verified: 2026-07-13.

- Cost Explorer `GetCostAndUsage`: https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html
- Cost result estimation: https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_ResultByTime.html
- Cost metric amount and unit: https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_MetricValue.html
- Cost Explorer API practices: https://docs.aws.amazon.com/cost-management/latest/userguide/ce-api-best-practices.html
- Cost Explorer pricing: https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/
- Cost Explorer access: https://docs.aws.amazon.com/cost-management/latest/userguide/ce-access.html
- Cost allocation tags: https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/cost-alloc-tags.html
- AWS Billing Conductor: https://docs.aws.amazon.com/billingconductor/latest/userguide/what-is-billingconductor.html
