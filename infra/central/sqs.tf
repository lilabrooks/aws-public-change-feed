resource "aws_sqs_queue" "delivery_dlq" {
  name                      = local.delivery_dlq_name
  fifo_queue                = true
  message_retention_seconds = 1209600

  tags = local.tags
}

resource "aws_sqs_queue" "delivery" {
  name                        = local.delivery_queue_name
  fifo_queue                  = true
  content_based_deduplication = false
  visibility_timeout_seconds  = local.worker_visibility_seconds
  message_retention_seconds   = 1209600

  tags = local.tags
}

resource "aws_sqs_queue_redrive_policy" "delivery" {
  queue_url = aws_sqs_queue.delivery.id

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.delivery_dlq.arn
    maxReceiveCount     = local.rate_control.queue_max_receive_count
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "delivery_dlq" {
  queue_url = aws_sqs_queue.delivery_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.delivery.arn]
  })
}

resource "aws_sqs_queue" "runtime_failures" {
  name                      = local.runtime_failure_queue_name
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true

  tags = local.tags
}

locals {
  # A policy-document data read is deferred when a referenced rule changes
  # state, even though its ARN is unchanged. Pure rendering keeps the exact
  # policy known during cost toggles; IAM changes remain outside that workflow.
  runtime_failure_queue_policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      local.watcher_runtime_enabled ? [{
        Sid       = "AllowExactWatcherSchedule"
        Effect    = "Allow"
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.runtime_failures.arn
        Principal = { Service = "events.amazonaws.com" }
        Condition = {
          ArnEquals    = { "aws:SourceArn" = aws_cloudwatch_event_rule.watcher[0].arn }
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        }
      }] : [],
      local.dispatcher_runtime_enabled ? [{
        Sid       = "AllowExactDispatcherSchedule"
        Effect    = "Allow"
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.runtime_failures.arn
        Principal = { Service = "events.amazonaws.com" }
        Condition = {
          ArnEquals    = { "aws:SourceArn" = aws_cloudwatch_event_rule.dispatcher[0].arn }
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        }
      }] : [],
      [{
        Sid       = "AllowExactReconcilerSchedule"
        Effect    = "Allow"
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.runtime_failures.arn
        Principal = { Service = "events.amazonaws.com" }
        Condition = {
          ArnEquals    = { "aws:SourceArn" = aws_cloudwatch_event_rule.reconciler.arn }
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        }
      }]
    )
  })
}

resource "aws_sqs_queue_policy" "runtime_failures" {
  queue_url = aws_sqs_queue.runtime_failures.id
  policy    = local.runtime_failure_queue_policy
}
