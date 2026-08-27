variable "expected_account_id" {
  description = "Exact AWS account authorized for the isolated exercise."
  type        = string
  default     = "667653114001"
  nullable    = false

  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "expected_account_id must be exactly 12 digits."
  }
}

variable "operational_sns_subscription_endpoints" {
  description = "Private email endpoint map for the isolated operational topic. Values enter Terraform state and remain outside Git."
  type        = map(string)
  default     = {}
  sensitive   = true
}

variable "application_artifact_sha256" {
  description = "Exact lowercase SHA-256 digest of the persistent dev application package. Null leaves every runtime undeployed."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.application_artifact_sha256 == null || can(regex("^[a-f0-9]{64}$", var.application_artifact_sha256))
    error_message = "application_artifact_sha256 must be null or exactly 64 lowercase hexadecimal characters."
  }

  validation {
    condition     = (var.application_artifact_sha256 == null) == (var.application_artifact_version_id == null)
    error_message = "application_artifact_sha256 and application_artifact_version_id must both be set or both be null."
  }
}

variable "application_artifact_version_id" {
  description = "Exact S3 VersionId of the persistent dev application package object."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.application_artifact_version_id == null || length(trimspace(var.application_artifact_version_id)) > 0
    error_message = "application_artifact_version_id must be null or a nonempty S3 version ID."
  }
}

variable "runtime_artifact_bucket_name" {
  description = "Persistent dev bucket holding the exact immutable application object."
  type        = string
  default     = "apcf-config-dev"
  nullable    = false

  validation {
    condition     = var.runtime_artifact_bucket_name == "apcf-config-dev"
    error_message = "the accepted first exercise must use the exact persistent dev artifact bucket."
  }
}

variable "exercise_load_triggers_enabled" {
  description = "Enable only the dispatcher schedule and worker mapping for the fixed load protocol. Watcher and reconciler schedules stay disabled."
  type        = bool
  default     = false
  nullable    = false
}

variable "enable_dynamodb_point_in_time_recovery" {
  description = "Whether the isolated tables use point-in-time recovery."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to isolated resources."
  type        = map(string)
  default = {
    project   = "aws-public-change-feed"
    lifecycle = "preflight"
  }
}
