locals {
  deployment = yamldecode(file(var.deployment_file))

  deployment_id = local.deployment.deployment_id
  region        = local.deployment.deployment_region

  tags = merge(var.tags, { deployment_id = local.deployment_id })

  config_bucket_name  = local.deployment.config_bucket_name
  release_prefix      = local.deployment.release_prefix
  active_versions_key = local.deployment.active_versions_object_key
  top_prefix          = dirname(local.active_versions_key)
  raw_snapshot_prefix = "${local.top_prefix}/raw-snapshots/"

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

  delivery_queue_name = "apcf-delivery-${local.deployment_id}.fifo"
  delivery_dlq_name   = "apcf-delivery-dlq-${local.deployment_id}.fifo"

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

  worker_timeout_seconds    = 300
  worker_batch_work_seconds = 60
  worker_visibility_seconds = local.worker_timeout_seconds + local.worker_batch_work_seconds + 60

  metrics_namespace = "AWSPublicChangeFeed/${local.deployment_id}"
}
