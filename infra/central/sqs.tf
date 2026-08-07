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
