locals {
  common_alarm_tags = {
    deployment_id = local.deployment_id
  }
}

resource "aws_cloudwatch_metric_alarm" "delivery_queue_age" {
  alarm_name          = "apcf-${local.deployment_id}-delivery-queue-age"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 5
  metric_name         = "ApproximateAgeOfOldestMessage"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 600
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = local.delivery_queue_name
  }

  alarm_description         = "Oldest queued delivery message exceeds 10 minutes. Deployment ${local.deployment_id}, region ${local.region}, queue ${local.delivery_queue_name}. See operations runbook 'Outbox or queue backlog'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

# Chapter 05 asks for oldest pending_queue, queued, or retryable work beyond the
# service objective. delivery_queue_age only sees work already in SQS, so it
# cannot observe a record stuck at pending_queue because the dispatcher is down.
# This reads the durable outbox instead.
resource "aws_cloudwatch_metric_alarm" "outbox_backlog_age" {
  alarm_name          = "apcf-${local.deployment_id}-outbox-backlog-age"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "OldestPendingDeliveryAgeSeconds"
  namespace           = local.metrics_namespace
  period              = 300
  statistic           = "Maximum"
  threshold           = 900
  treat_missing_data  = "notBreaching"

  alarm_description         = "Oldest unresolved outbox work exceeds 15 minutes. Deployment ${local.deployment_id}, region ${local.region}, table ${local.runtime_delivery_table}. See operations runbook 'Outbox or queue backlog'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "delivery_dlq_depth" {
  alarm_name          = "apcf-${local.deployment_id}-delivery-dlq-depth"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = local.delivery_dlq_name
  }

  alarm_description         = "Messages present on the delivery DLQ ${local.delivery_dlq_name}. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'DLQ response'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "runtime_failure_queue_depth" {
  alarm_name          = "apcf-${local.deployment_id}-runtime-failure-queue-depth"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = local.runtime_failure_queue_name
  }

  alarm_description         = "A scheduled runtime event exhausted retries. Deployment ${local.deployment_id}, region ${local.region}, queue ${local.runtime_failure_queue_name}. See operations runbook 'Scheduled runtime failure'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "feed_watcher_errors" {
  count = local.watcher_trigger_enabled ? 1 : 0

  alarm_name          = "apcf-${local.deployment_id}-feed-watcher-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 900
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = local.function_names.watcher
  }

  alarm_description         = "Feed watcher errors breached for two consecutive 15-minute periods. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Feed stale or fetch failing'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "watcher_incomplete_runs" {
  count = local.watcher_trigger_enabled ? 1 : 0

  alarm_name          = "apcf-${local.deployment_id}-watcher-incomplete-runs"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  metric_name         = "IncompleteRuns"
  namespace           = local.metrics_namespace
  period              = 900
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    Function = local.function_names.watcher
  }

  alarm_description         = "Feed watcher incompletion breached for two consecutive 15-minute periods. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Feed stale or fetch failing'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "watcher_fault" {
  count = local.watcher_trigger_enabled ? 1 : 0

  alarm_name          = "apcf-${local.deployment_id}-watcher-fault"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  metric_name         = "WatcherFaults"
  namespace           = local.metrics_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    Function = local.function_names.watcher
  }

  alarm_description         = "The feed watcher stopped on an unexpected internal fault. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Feed stale or fetch failing'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "dispatcher_errors" {
  count = local.dispatcher_trigger_enabled ? 1 : 0

  alarm_name          = "apcf-${local.deployment_id}-outbox-dispatcher-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = local.function_names.dispatcher
  }

  alarm_description         = "Outbox dispatcher function reported errors. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Outbox or queue backlog'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "dispatcher_heartbeat" {
  count = local.dispatcher_trigger_enabled ? 1 : 0

  alarm_name          = "apcf-${local.deployment_id}-outbox-dispatcher-heartbeat"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  metric_name         = "Heartbeat"
  namespace           = local.metrics_namespace
  period              = 60
  statistic           = "Maximum"
  threshold           = 1
  treat_missing_data  = "breaching"

  dimensions = {
    Function = local.function_names.dispatcher
  }

  alarm_description         = "Outbox dispatcher heartbeat missing for three minutes. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Outbox or queue backlog'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "worker_errors" {
  alarm_name          = "apcf-${local.deployment_id}-slack-worker-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = local.function_names.worker
  }

  alarm_description         = "Slack worker function reported errors. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Slack retryable or terminal failure'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "reconciler_errors" {
  alarm_name          = "apcf-${local.deployment_id}-recovery-reconciler-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = local.function_names.reconciler
  }

  alarm_description         = "Recovery reconciler function reported errors. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Outbox or queue backlog'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "source_state_write_throttles" {
  alarm_name          = "apcf-${local.deployment_id}-source-state-write-throttles"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "WriteThrottleEvents"
  namespace           = "AWS/DynamoDB"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    TableName = local.runtime_source_state_table
  }

  alarm_description         = "Source-state table ${local.runtime_source_state_table} reported write throttles. Deployment ${local.deployment_id}, region ${local.region}."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "delivery_write_throttles" {
  alarm_name          = "apcf-${local.deployment_id}-delivery-write-throttles"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "WriteThrottleEvents"
  namespace           = "AWS/DynamoDB"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    TableName = local.runtime_delivery_table
  }

  alarm_description         = "Delivery table ${local.runtime_delivery_table} reported write throttles. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Outbox or queue backlog'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "feed_watcher_heartbeat" {
  count = local.watcher_trigger_enabled ? 1 : 0

  alarm_name          = "apcf-${local.deployment_id}-feed-watcher-heartbeat"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  metric_name         = "Heartbeat"
  namespace           = local.metrics_namespace
  period              = 900
  statistic           = "Maximum"
  threshold           = 1
  treat_missing_data  = "breaching"

  dimensions = {
    Function = local.function_names.watcher
  }

  alarm_description         = "Feed watcher heartbeat missing for 45 minutes. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Feed appears quiet'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "reconciler_heartbeat" {
  count = local.reconciler_trigger_enabled ? 1 : 0

  alarm_name          = "apcf-${local.deployment_id}-recovery-reconciler-heartbeat"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  metric_name         = "Heartbeat"
  namespace           = local.metrics_namespace
  period              = 300
  statistic           = "Maximum"
  threshold           = 1
  treat_missing_data  = "breaching"

  dimensions = {
    Function = local.function_names.reconciler
  }

  alarm_description         = "Recovery reconciler heartbeat missing for 15 minutes. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Outbox or queue backlog'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "delivery_unknown" {
  alarm_name          = "apcf-${local.deployment_id}-delivery-unknown"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "DeliveryUnknown"
  namespace           = local.metrics_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_description         = "A delivery entered the delivery_unknown state. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Delivery unknown'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "application_version_mismatch" {
  alarm_name          = "apcf-${local.deployment_id}-application-version-mismatch"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApplicationVersionMismatch"
  namespace           = local.metrics_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_description         = "Queued work requires another application package. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Application package rollout and rollback'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "artifact_unavailable" {
  alarm_name          = "apcf-${local.deployment_id}-application-artifact-unavailable"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ArtifactUnavailable"
  namespace           = local.metrics_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_description         = "Queued evidence references an unavailable application artifact. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Application package rollout and rollback'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "artifact_availability_check_failed" {
  alarm_name          = "apcf-${local.deployment_id}-application-artifact-check-failed"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ArtifactAvailabilityCheckFailed"
  namespace           = local.metrics_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_description         = "The worker could not determine whether a required application artifact is retained. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Application package rollout and rollback'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "worker_fault" {
  alarm_name          = "apcf-${local.deployment_id}-worker-fault"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "WorkerFault"
  namespace           = local.metrics_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_description         = "The worker returned a FIFO suffix after an unexpected internal fault. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Outbox or queue backlog'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "dispatch_unknown_outcome" {
  alarm_name          = "apcf-${local.deployment_id}-dispatch-unknown-outcome"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "DispatchUnknownOutcome"
  namespace           = local.metrics_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_description         = "An SQS send outcome was unknown and remains recoverable. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Outbox or queue backlog'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "recovery_observation_saturated" {
  alarm_name          = "apcf-${local.deployment_id}-recovery-observation-saturated"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "StateObservationSaturated"
  namespace           = local.metrics_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_description         = "A recovery state exceeded its bounded 100-record observation. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Outbox or queue backlog'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "recovery_repair_limit" {
  alarm_name          = "apcf-${local.deployment_id}-recovery-repair-limit"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "RecoveryRepairLimitReached"
  namespace           = local.metrics_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_description         = "A recovery run left repairable work beyond its 100-record limit. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Outbox or queue backlog'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "reconciler_fault" {
  alarm_name          = "apcf-${local.deployment_id}-reconciler-fault"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ReconcilerFault"
  namespace           = local.metrics_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_description         = "The recovery reconciler stopped on an unexpected internal fault. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Scheduled runtime failure'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "release_verification_failures" {
  alarm_name          = "apcf-${local.deployment_id}-release-verification-failures"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ReleaseVerificationFailures"
  namespace           = local.metrics_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_description = "Release verification failed for an exact versioned object. Diagnostic only; WatcherFaults owns notification paging. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Release failure or rollback'."
  tags              = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "raw_snapshot_failures" {
  alarm_name          = "apcf-${local.deployment_id}-raw-snapshot-failures"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "RawSnapshotFailures"
  namespace           = local.metrics_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_description         = "A raw feed snapshot write failed. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Feed stale or fetch failing'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "terminal_failures" {
  alarm_name          = "apcf-${local.deployment_id}-terminal-failures"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "TerminalFailure"
  namespace           = local.metrics_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_description         = "A delivery entered the failed_terminal state. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Slack retryable or terminal failure'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

# One alarm across all feeds, not one alarm per feed. The feed list lives in
# config.yaml, which is a release artifact this root never reads, so Terraform
# cannot enumerate feeds at plan time. The watcher emits a dimensionless
# MaxFeedStalenessSeconds aggregate for this alarm and FeedStalenessSeconds
# with a FeedName dimension for per-feed attribution in the dashboard.
resource "aws_cloudwatch_metric_alarm" "feed_staleness" {
  alarm_name          = "apcf-${local.deployment_id}-feed-staleness"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "MaxFeedStalenessSeconds"
  namespace           = local.metrics_namespace
  period              = 900
  statistic           = "Maximum"
  threshold           = 3600
  treat_missing_data  = "notBreaching"

  alarm_description         = "Maximum staleness across all feeds exceeds 60 minutes. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Feed stale or fetch failing'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}
