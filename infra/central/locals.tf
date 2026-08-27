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
  runtime_artifact_bucket_name = (
    var.runtime_artifact_bucket_name == null ? aws_s3_bucket.config.id : var.runtime_artifact_bucket_name
  )

  secret_store = local.deployment.secret_store

  log_retention_days = local.deployment.log_retention_days
  sns_topic_name     = local.deployment.operational_sns_topic_name

  operational_sns_subscriptions = {
    for subscription in local.deployment.operational_sns_subscriptions : subscription.alias => subscription
  }
  operational_sns_subscription_aliases = toset(keys(local.operational_sns_subscriptions))
  operational_sns_endpoint_aliases     = toset(keys(var.operational_sns_subscription_endpoints))

  rate_control      = local.deployment.slack.rate_control
  feed_fetch_policy = local.deployment.feed_fetch_policy

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
  worker_trigger_requested = (
    var.worker_trigger_enabled_override == null ? var.delivery_triggers_enabled : var.worker_trigger_enabled_override
  )
  worker_trigger_enabled = local.worker_runtime_enabled && local.worker_trigger_requested
  worker_artifact_key    = var.worker_artifact_sha256 == null ? null : "${local.application_artifact_prefix}/${var.worker_artifact_sha256}.zip"
  application_version    = var.worker_artifact_sha256 == null ? null : "sha256:${var.worker_artifact_sha256}"

  watcher_timeout_seconds        = 300
  watcher_reserved_concurrency   = 1
  watcher_lease_seconds          = 360
  watcher_schedule_expression    = "rate(15 minutes)"
  watcher_maximum_retry_attempts = 2
  watcher_maximum_event_age      = 900
  watcher_runtime_enabled        = var.watcher_artifact_sha256 != null && var.watcher_artifact_version_id != null
  watcher_trigger_requested = (
    var.watcher_trigger_enabled_override == null ? var.delivery_triggers_enabled : var.watcher_trigger_enabled_override
  )
  watcher_trigger_enabled     = local.watcher_runtime_enabled && local.watcher_trigger_requested
  watcher_artifact_key        = var.watcher_artifact_sha256 == null ? null : "${local.application_artifact_prefix}/${var.watcher_artifact_sha256}.zip"
  watcher_application_version = var.watcher_artifact_sha256 == null ? null : "sha256:${var.watcher_artifact_sha256}"

  dispatcher_timeout_seconds        = 60
  dispatcher_reserved_concurrency   = 1
  dispatcher_schedule_expression    = "rate(1 minute)"
  dispatcher_maximum_retry_attempts = 2
  dispatcher_maximum_event_age      = 300
  dispatcher_runtime_enabled        = var.dispatcher_artifact_sha256 != null && var.dispatcher_artifact_version_id != null
  dispatcher_trigger_requested = (
    var.dispatcher_trigger_enabled_override == null ? var.delivery_triggers_enabled : var.dispatcher_trigger_enabled_override
  )
  dispatcher_trigger_enabled     = local.dispatcher_runtime_enabled && local.dispatcher_trigger_requested
  dispatcher_artifact_key        = var.dispatcher_artifact_sha256 == null ? null : "${local.application_artifact_prefix}/${var.dispatcher_artifact_sha256}.zip"
  dispatcher_application_version = var.dispatcher_artifact_sha256 == null ? null : "sha256:${var.dispatcher_artifact_sha256}"

  reconciler_timeout_seconds        = 60
  reconciler_reserved_concurrency   = 1
  reconciler_repair_limit           = 100
  reconciler_observation_limit      = 101
  reconciler_stale_queued_seconds   = 600
  reconciler_schedule_expression    = "rate(5 minutes)"
  reconciler_maximum_retry_attempts = 2
  reconciler_maximum_event_age      = 300
  reconciler_runtime_enabled        = var.reconciler_artifact_sha256 != null && var.reconciler_artifact_version_id != null
  reconciler_trigger_enabled        = local.reconciler_runtime_enabled && var.reconciler_trigger_enabled
  reconciler_artifact_key           = var.reconciler_artifact_sha256 == null ? null : "${local.application_artifact_prefix}/${var.reconciler_artifact_sha256}.zip"
  reconciler_application_version    = var.reconciler_artifact_sha256 == null ? null : "sha256:${var.reconciler_artifact_sha256}"

  metrics_namespace = "AWSPublicChangeFeed/${local.deployment_id}"
}
