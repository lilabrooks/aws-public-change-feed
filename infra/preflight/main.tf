locals {
  deployment_path = abspath("${path.module}/deployment.yaml")
  deployment      = yamldecode(file(local.deployment_path))
  state_key       = "apcf/preflight/terraform.tfstate"
}

resource "terraform_data" "identity_guard" {
  input = {
    account_id         = var.expected_account_id
    deployment_id      = local.deployment.deployment_id
    region             = local.deployment.deployment_region
    config_bucket_name = local.deployment.config_bucket_name
    state_key          = local.state_key
  }

  lifecycle {
    precondition {
      condition     = local.deployment.deployment_id == "preflight"
      error_message = "the isolated exercise root requires deployment_id preflight."
    }

    precondition {
      condition     = local.deployment.deployment_region == "us-east-1"
      error_message = "the accepted first exercise region is us-east-1."
    }

    precondition {
      condition     = local.deployment.config_bucket_name == "apcf-config-preflight-${var.expected_account_id}"
      error_message = "the isolated configuration bucket must include the exact authorized account ID."
    }

    precondition {
      condition     = local.deployment.operational_sns_topic_name == "apcf-preflight-operations"
      error_message = "the isolated exercise must use the dedicated operational topic."
    }
  }
}

module "runtime" {
  source = "../central"

  deployment_file                        = local.deployment_path
  operational_sns_subscription_endpoints = var.operational_sns_subscription_endpoints
  enable_dynamodb_point_in_time_recovery = var.enable_dynamodb_point_in_time_recovery
  tags                                   = var.tags

  preflight_mode               = true
  runtime_artifact_bucket_name = var.runtime_artifact_bucket_name

  worker_artifact_sha256         = var.application_artifact_sha256
  worker_artifact_version_id     = var.application_artifact_version_id
  watcher_artifact_sha256        = var.application_artifact_sha256
  watcher_artifact_version_id    = var.application_artifact_version_id
  dispatcher_artifact_sha256     = var.application_artifact_sha256
  dispatcher_artifact_version_id = var.application_artifact_version_id
  reconciler_artifact_sha256     = var.application_artifact_sha256
  reconciler_artifact_version_id = var.application_artifact_version_id

  delivery_triggers_enabled           = false
  reconciler_trigger_enabled          = false
  watcher_trigger_enabled_override    = false
  dispatcher_trigger_enabled_override = var.exercise_load_triggers_enabled
  worker_trigger_enabled_override     = var.exercise_load_triggers_enabled
}
