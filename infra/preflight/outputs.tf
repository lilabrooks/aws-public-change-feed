output "preflight_identity" {
  description = "Exact isolated deployment identity consumed by the preview-first runner."
  value       = terraform_data.identity_guard.output
}

output "config_bucket_name" {
  value = module.runtime.config_bucket_name
}

output "release_prefix" {
  value = module.runtime.release_prefix
}

output "runtime_artifact_bucket_name" {
  value = module.runtime.runtime_artifact_bucket_name
}

output "application_artifact_prefix" {
  value = module.runtime.application_artifact_prefix
}

output "application_versions" {
  value = {
    worker     = module.runtime.worker_application_version
    watcher    = module.runtime.watcher_application_version
    dispatcher = module.runtime.dispatcher_application_version
    reconciler = module.runtime.reconciler_application_version
  }
}

output "source_state_table" {
  value = module.runtime.source_state_table
}

output "delivery_table" {
  value = module.runtime.delivery_table
}

output "delivery_index_name" {
  value = module.runtime.delivery_index_name
}

output "delivery_queue" {
  value = module.runtime.delivery_queue
}

output "delivery_queue_arn" {
  value = module.runtime.delivery_queue_arn
}

output "delivery_dlq" {
  value = module.runtime.delivery_dlq
}

output "runtime_failure_queue" {
  value = module.runtime.runtime_failure_queue
}

output "operational_sns_topic_arn" {
  value = module.runtime.operational_sns_topic_arn
}

output "slack_secret_ids" {
  value = module.runtime.slack_secret_ids
}

output "roles" {
  value = module.runtime.roles
}

output "function_names" {
  value = module.runtime.function_names
}

output "runtime_trigger_states" {
  value = module.runtime.runtime_trigger_states
}

output "dashboard_name" {
  value = module.runtime.dashboard_name
}
