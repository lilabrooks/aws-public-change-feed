locals {
  runbook = "https://github.com/lilabrooks/aws-public-change-feed/blob/main/docs/runbooks/operations.md"

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

  alarm_description         = "Oldest unresolved outbox work exceeds 15 minutes. Deployment ${local.deployment_id}, region ${local.region}, table ${local.delivery_table}. See operations runbook 'Outbox or queue backlog'."
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

resource "aws_cloudwatch_metric_alarm" "feed_watcher_errors" {
  alarm_name          = "apcf-${local.deployment_id}-feed-watcher-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = local.function_names.watcher
  }

  alarm_description         = "Feed watcher function reported errors. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Feed stale or fetch failing'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "dispatcher_errors" {
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
    TableName = local.source_state_table
  }

  alarm_description         = "Source-state table ${local.source_state_table} reported write throttles. Deployment ${local.deployment_id}, region ${local.region}."
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
    TableName = local.delivery_table
  }

  alarm_description         = "Delivery table ${local.delivery_table} reported write throttles. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Outbox or queue backlog'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
}

resource "aws_cloudwatch_metric_alarm" "feed_watcher_heartbeat" {
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

  alarm_description         = "Release verification failed for an exact versioned object. Deployment ${local.deployment_id}, region ${local.region}. See operations runbook 'Release failure or rollback'."
  alarm_actions             = [aws_sns_topic.operations.arn]
  ok_actions                = [aws_sns_topic.operations.arn]
  insufficient_data_actions = [aws_sns_topic.operations.arn]
  tags                      = local.common_alarm_tags
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
