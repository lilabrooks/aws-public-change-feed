output "config_bucket_name" {
  description = "Versioned configuration bucket holding releases, the active manifest, and raw feed snapshots."
  value       = aws_s3_bucket.config.id
}

output "config_bucket_arn" {
  description = "ARN of the configuration bucket."
  value       = aws_s3_bucket.config.arn
}

output "raw_snapshot_prefix" {
  description = "Key prefix the feed watcher writes raw feed snapshots to. The S3 SnapshotStore adapter reads this rather than reconstructing it."
  value       = local.raw_snapshot_prefix
}

output "application_artifact_prefix" {
  description = "Append-only prefix for content-addressed deployable Lambda packages."
  value       = local.application_artifact_prefix
}

output "runtime_artifact_bucket_name" {
  description = "Bucket holding the exact immutable package version selected for Lambda code."
  value       = local.runtime_artifact_bucket_name
}

output "worker_application_version" {
  description = "Exact sha256 application version injected into the deployed worker, or null when undeployed."
  value       = local.application_version
}

output "watcher_application_version" {
  description = "Exact sha256 application version injected into the deployed watcher, or null when undeployed."
  value       = local.watcher_application_version
}

output "dispatcher_application_version" {
  description = "Exact sha256 package version selected for the outbox dispatcher, or null when undeployed."
  value       = local.dispatcher_application_version
}

output "reconciler_application_version" {
  description = "Exact sha256 package version selected for the recovery reconciler, or null when undeployed."
  value       = local.reconciler_application_version
}

output "release_prefix" {
  description = "Key prefix holding write-once release objects."
  value       = "${local.release_prefix}/"
}

output "source_state_table" {
  description = "DynamoDB source-state table currently bound to the runtime."
  value       = local.runtime_source_state_table
}

output "delivery_table" {
  description = "DynamoDB delivery table currently bound to the runtime."
  value       = local.runtime_delivery_table
}

output "primary_source_state_table" {
  description = "Terraform-owned source-state table retained through an ADR-027 recovery cutover."
  value       = aws_dynamodb_table.source_state.name
}

output "primary_delivery_table" {
  description = "Terraform-owned delivery table retained through an ADR-027 recovery cutover."
  value       = aws_dynamodb_table.delivery.name
}

output "dynamodb_recovery" {
  description = "ADR-027 PITR and exact runtime cutover selection."
  value = {
    pitr_enabled         = var.enable_dynamodb_point_in_time_recovery
    recovery_period_days = var.dynamodb_recovery_period_days
    cutover_enabled      = local.recovery_cutover_enabled
    plan_sha256          = local.recovery_cutover_enabled ? var.dynamodb_recovery_cutover.plan_sha256 : null
  }
}

output "delivery_index_name" {
  description = "Global secondary index the dispatcher and reconciler query for due work."
  value       = local.delivery_index_name
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

output "runtime_failure_queue" {
  description = "Encrypted standard queue holding exhausted scheduled-runtime target events."
  value       = aws_sqs_queue.runtime_failures.name
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
  description = "ARNs of the least-privilege publication, retirement, and runtime roles."
  value = {
    release_publisher                = aws_iam_role.release_publisher.arn
    dynamodb_recovery                = aws_iam_role.dynamodb_recovery.arn
    dynamodb_recovery_evidence       = aws_iam_role.dynamodb_recovery_evidence.arn
    application_artifact_retirement  = aws_iam_role.application_artifact_retirement.arn
    source_state_retention_migration = try(aws_iam_role.source_state_retention_migration[0].arn, null)
    source_state_retirement          = aws_iam_role.source_state_retirement.arn
    source_replay                    = aws_iam_role.source_replay.arn
    feed_watcher                     = aws_iam_role.feed_watcher.arn
    shadow_evaluator                 = aws_iam_role.shadow_evaluator.arn
    shadow_invoker                   = aws_iam_role.shadow_invoker.arn
    outbox_dispatcher                = aws_iam_role.outbox_dispatcher.arn
    slack_worker                     = aws_iam_role.slack_worker.arn
    recovery_reconciler              = aws_iam_role.recovery_reconciler.arn
  }
}

output "function_names" {
  description = "Lambda names; the watcher, shadow evaluator, dispatcher, Slack worker, and reconciler exist when their exact artifact inputs are supplied."
  value       = local.function_names
}

output "runtime_trigger_states" {
  description = "Effective trigger state for each conditional runtime."
  value = {
    watcher    = local.watcher_trigger_enabled
    dispatcher = local.dispatcher_trigger_enabled
    worker     = local.worker_trigger_enabled
    reconciler = local.reconciler_trigger_enabled
  }
}

output "watcher_execution_paused" {
  description = "Whether the watcher reserved-concurrency pause is selected."
  value       = var.watcher_execution_paused
}

output "dashboard_name" {
  description = "CloudWatch operations dashboard name."
  value       = aws_cloudwatch_dashboard.operations.dashboard_name
}
