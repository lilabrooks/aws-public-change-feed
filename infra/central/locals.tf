locals {
  deployment = yamldecode(file(var.deployment_file))

  deployment_id = local.deployment.deployment_id
  region        = local.deployment.deployment_region

  tags = merge(var.tags, { deployment_id = local.deployment_id })

  config_bucket_name  = local.deployment.config_bucket_name
  release_prefix      = local.deployment.release_prefix
  active_versions_key = local.deployment.active_versions_object_key
  top_prefix          = dirname(local.active_versions_key)

  # Chapter 03 "Configuration bucket layout" fixes this prefix. It is not a free
  # choice: the watcher's s3:PutObject grant and the snapshot lifecycle rules are
  # both scoped to it, and the S3 SnapshotStore adapter must write here. The
  # raw_snapshot_prefix output publishes it so the adapter reads it rather than
  # reconstructing the string.
  raw_snapshot_prefix         = "${local.top_prefix}/raw-snapshots/"
  application_artifact_prefix = "${local.top_prefix}/application-artifacts"

  secret_store = local.deployment.secret_store

  log_retention_days = local.deployment.log_retention_days
  sns_topic_name     = local.deployment.operational_sns_topic_name

  rate_control = local.deployment.slack.rate_control

  slack_secret_ids = distinct(compact(concat(
    [try(local.deployment.slack.bot_token_secret_id, null)],
    [for route in values(local.deployment.slack.routes) : lookup(route, "credential_secret_id", null)],
  )))

  secret_read_actions = local.secret_store == "secrets_manager" ? ["secretsmanager:GetSecretValue"] : ["ssm:GetParameter"]
  secret_arns         = local.secret_store == "secrets_manager" ? aws_secretsmanager_secret.slack_credentials[*].arn : aws_ssm_parameter.slack_credentials[*].arn

  source_state_table = "apcf-source-state-${local.deployment_id}"
  delivery_table     = "apcf-delivery-${local.deployment_id}"

  # ADR-007: the dispatcher and reconciler read due work through this index.
  delivery_index_name = "status-next-action-index"
  delivery_index_arn  = "${aws_dynamodb_table.delivery.arn}/index/${local.delivery_index_name}"

  delivery_queue_name        = "apcf-delivery-${local.deployment_id}.fifo"
  delivery_dlq_name          = "apcf-delivery-dlq-${local.deployment_id}.fifo"
  runtime_failure_queue_name = "apcf-runtime-failures-${local.deployment_id}"

  function_names = {
    watcher    = "apcf-${local.deployment_id}-feed-watcher"
    dispatcher = "apcf-${local.deployment_id}-outbox-dispatcher"
    worker     = "apcf-${local.deployment_id}-slack-worker"
    reconciler = "apcf-${local.deployment_id}-recovery-reconciler"
  }

  role_names = {
    publisher  = "apcf-${local.deployment_id}-release-publisher"
    watcher    = "apcf-${local.deployment_id}-feed-watcher"
    dispatcher = "apcf-${local.deployment_id}-outbox-dispatcher"
    worker     = "apcf-${local.deployment_id}-slack-worker"
    reconciler = "apcf-${local.deployment_id}-recovery-reconciler"
  }

  worker_timeout_seconds             = 300
  worker_batch_size                  = 10
  worker_batch_window_seconds        = 0
  worker_safety_reserve_milliseconds = 30000
  worker_lease_duration_seconds      = local.worker_timeout_seconds
  max_delivery_request_bytes         = 245760
  worker_visibility_seconds          = 6 * local.worker_timeout_seconds + local.worker_batch_window_seconds
  worker_runtime_enabled             = var.worker_artifact_sha256 != null && var.worker_artifact_version_id != null
  worker_artifact_key                = var.worker_artifact_sha256 == null ? null : "${local.application_artifact_prefix}/${var.worker_artifact_sha256}.zip"
  application_version                = var.worker_artifact_sha256 == null ? null : "sha256:${var.worker_artifact_sha256}"

  reconciler_timeout_seconds        = 60
  reconciler_reserved_concurrency   = 1
  reconciler_repair_limit           = 100
  reconciler_observation_limit      = 101
  reconciler_stale_queued_seconds   = 600
  reconciler_schedule_expression    = "rate(5 minutes)"
  reconciler_maximum_retry_attempts = 2
  reconciler_maximum_event_age      = 300
  reconciler_runtime_enabled        = var.reconciler_artifact_sha256 != null && var.reconciler_artifact_version_id != null
  reconciler_artifact_key           = var.reconciler_artifact_sha256 == null ? null : "${local.application_artifact_prefix}/${var.reconciler_artifact_sha256}.zip"
  reconciler_application_version    = var.reconciler_artifact_sha256 == null ? null : "sha256:${var.reconciler_artifact_sha256}"

  metrics_namespace = "AWSPublicChangeFeed/${local.deployment_id}"
}
