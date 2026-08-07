output "config_bucket_name" {
  description = "Versioned configuration bucket holding releases, the active manifest, and raw feed snapshots."
  value       = aws_s3_bucket.config.id
}

output "config_bucket_arn" {
  description = "ARN of the configuration bucket."
  value       = aws_s3_bucket.config.arn
}

output "source_state_table" {
  description = "DynamoDB source-state table for feed checkpoints and announcement records."
  value       = aws_dynamodb_table.source_state.name
}

output "delivery_table" {
  description = "DynamoDB delivery table for candidates, outbox work, pacing, and outcomes."
  value       = aws_dynamodb_table.delivery.name
}

output "delivery_queue" {
  description = "Encrypted SQS FIFO queue name."
  value       = aws_sqs_queue.delivery.name
}

output "delivery_queue_arn" {
  description = "ARN of the delivery FIFO queue."
  value       = aws_sqs_queue.delivery.arn
}

output "delivery_dlq" {
  description = "FIFO dead-letter queue name."
  value       = aws_sqs_queue.delivery_dlq.name
}

output "operational_sns_topic_arn" {
  description = "ARN of the operational notification topic."
  value       = aws_sns_topic.operations.arn
}

output "slack_secret_ids" {
  description = "Slack credential identifiers provisioned as secret containers."
  value       = local.slack_secret_ids
}

output "roles" {
  description = "ARNs of the five least-privilege runtime roles."
  value = {
    release_publisher   = aws_iam_role.release_publisher.arn
    feed_watcher        = aws_iam_role.feed_watcher.arn
    outbox_dispatcher   = aws_iam_role.outbox_dispatcher.arn
    slack_worker        = aws_iam_role.slack_worker.arn
    recovery_reconciler = aws_iam_role.recovery_reconciler.arn
  }
}

output "function_names" {
  description = "Planned Lambda function names (runtime packages not yet deployed)."
  value       = local.function_names
}

output "dashboard_name" {
  description = "CloudWatch operations dashboard name."
  value       = aws_cloudwatch_dashboard.operations.dashboard_name
}
