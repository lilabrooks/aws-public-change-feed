resource "aws_cloudwatch_dashboard" "operations" {
  dashboard_name = "apcf-${local.deployment_id}-operations"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 1
        properties = {
          markdown = "## AWS Public Change Alerting (${local.deployment_id})"
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 1
        width  = 12
        height = 6
        properties = {
          title       = "Delivery queue"
          view        = "timeSeries"
          stacked     = false
          region      = local.region
          annotations = {}
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", local.delivery_queue_name, { "stat" : "Maximum", "id" : "m1", "label" : "visible" }],
            ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", local.delivery_queue_name, { "stat" : "Maximum", "id" : "m2", "label" : "oldest age (s)", "yAxis" : "right" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 1
        width  = 12
        height = 6
        properties = {
          title       = "Delivery DLQ"
          view        = "timeSeries"
          stacked     = false
          region      = local.region
          annotations = {}
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", local.delivery_dlq_name, { "stat" : "Maximum", "id" : "m1", "label" : "dlq visible" }],
            ["AWS/SQS", "NumberOfMessagesReceived", "QueueName", local.delivery_dlq_name, { "stat" : "Sum", "id" : "m2", "label" : "dlq receives", "yAxis" : "right" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 7
        width  = 12
        height = 6
        properties = {
          title       = "DynamoDB write throttles"
          view        = "timeSeries"
          stacked     = false
          region      = local.region
          annotations = {}
          metrics = [
            ["AWS/DynamoDB", "WriteThrottleEvents", "TableName", local.source_state_table, { "stat" : "Sum", "id" : "s1", "label" : "source-state" }],
            ["AWS/DynamoDB", "WriteThrottleEvents", "TableName", local.delivery_table, { "stat" : "Sum", "id" : "d1", "label" : "delivery" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 7
        width  = 12
        height = 6
        properties = {
          title       = "Lambda errors"
          view        = "timeSeries"
          stacked     = false
          region      = local.region
          annotations = {}
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", local.function_names.watcher, { "stat" : "Sum", "id" : "w", "label" : "watcher" }],
            ["AWS/Lambda", "Errors", "FunctionName", local.function_names.dispatcher, { "stat" : "Sum", "id" : "d", "label" : "dispatcher" }],
            ["AWS/Lambda", "Errors", "FunctionName", local.function_names.worker, { "stat" : "Sum", "id" : "s", "label" : "worker" }],
            ["AWS/Lambda", "Errors", "FunctionName", local.function_names.reconciler, { "stat" : "Sum", "id" : "r", "label" : "reconciler" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 13
        width  = 24
        height = 3
        properties = {
          title       = "Feed freshness"
          view        = "timeSeries"
          stacked     = false
          region      = local.region
          annotations = {}
          metrics = [
            [local.metrics_namespace, "MaxFeedStalenessSeconds", { "stat" : "Maximum", "id" : "fs", "label" : "max feed staleness (s)" }],
            [{ "expression" : "SEARCH('{${local.metrics_namespace},FeedName} MetricName=\"FeedStalenessSeconds\"', 'Maximum', 900)", "id" : "feeds", "label" : "", "region" : local.region }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 16
        width  = 24
        height = 6
        properties = {
          title       = "Service heartbeats and signals"
          view        = "timeSeries"
          stacked     = false
          region      = local.region
          annotations = {}
          metrics = [
            [local.metrics_namespace, "Heartbeat", "Function", local.function_names.watcher, { "stat" : "Maximum", "id" : "hw", "label" : "watcher heartbeat" }],
            [local.metrics_namespace, "Heartbeat", "Function", local.function_names.reconciler, { "stat" : "Maximum", "id" : "hr", "label" : "reconciler heartbeat" }],
            [local.metrics_namespace, "DeliveryUnknown", { "stat" : "Sum", "id" : "du", "label" : "delivery unknown", "yAxis" : "right" }],
            [local.metrics_namespace, "TerminalFailure", { "stat" : "Sum", "id" : "tf", "label" : "terminal delivery", "yAxis" : "right" }],
            [local.metrics_namespace, "ApplicationVersionMismatch", { "stat" : "Sum", "id" : "av", "label" : "application mismatch", "yAxis" : "right" }],
            [local.metrics_namespace, "ArtifactUnavailable", { "stat" : "Sum", "id" : "au", "label" : "artifact unavailable", "yAxis" : "right" }],
            [local.metrics_namespace, "ArtifactAvailabilityCheckFailed", { "stat" : "Sum", "id" : "ac", "label" : "artifact check failed", "yAxis" : "right" }],
            [local.metrics_namespace, "WorkerFault", { "stat" : "Sum", "id" : "wf", "label" : "worker fault", "yAxis" : "right" }],
            [local.metrics_namespace, "DispatchUnknownOutcome", { "stat" : "Sum", "id" : "uo", "label" : "dispatch unknown", "yAxis" : "right" }],
          ]
        }
      },
    ]
  })
}
