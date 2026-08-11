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

data "aws_iam_policy_document" "runtime_failure_queue" {
  dynamic "statement" {
    for_each = local.watcher_runtime_enabled ? [1] : []
    content {
      sid     = "AllowExactWatcherSchedule"
      actions = ["sqs:SendMessage"]
      resources = [
        aws_sqs_queue.runtime_failures.arn,
      ]

      principals {
        type        = "Service"
        identifiers = ["events.amazonaws.com"]
      }

      condition {
        test     = "ArnEquals"
        variable = "aws:SourceArn"
        values   = [aws_cloudwatch_event_rule.watcher[0].arn]
      }

      condition {
        test     = "StringEquals"
        variable = "aws:SourceAccount"
        values   = [data.aws_caller_identity.current.account_id]
      }
    }
  }

  dynamic "statement" {
    for_each = local.dispatcher_runtime_enabled ? [1] : []
    content {
      sid     = "AllowExactDispatcherSchedule"
      actions = ["sqs:SendMessage"]
      resources = [
        aws_sqs_queue.runtime_failures.arn,
      ]

      principals {
        type        = "Service"
        identifiers = ["events.amazonaws.com"]
      }

      condition {
        test     = "ArnEquals"
        variable = "aws:SourceArn"
        values   = [aws_cloudwatch_event_rule.dispatcher[0].arn]
      }

      condition {
        test     = "StringEquals"
        variable = "aws:SourceAccount"
        values   = [data.aws_caller_identity.current.account_id]
      }
    }
  }

  statement {
    sid     = "AllowExactReconcilerSchedule"
    actions = ["sqs:SendMessage"]
    resources = [
      aws_sqs_queue.runtime_failures.arn,
    ]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.reconciler.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sqs_queue_policy" "runtime_failures" {
  queue_url = aws_sqs_queue.runtime_failures.id
  policy    = data.aws_iam_policy_document.runtime_failure_queue.json
}
