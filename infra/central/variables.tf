variable "deployment_file" {
  description = "Path to the deployment configuration input, relative to this root."
  type        = string
  default     = "deployment.yaml"
}

variable "operational_sns_subscription_endpoints" {
  description = "Private map from each reviewed operational SNS subscription alias to its endpoint. Values enter Terraform state and must be supplied outside Git."
  type        = map(string)
  default     = {}
  sensitive   = true

  validation {
    condition = alltrue([
      for endpoint in values(var.operational_sns_subscription_endpoints) : length(trimspace(endpoint)) > 0
    ])
    error_message = "operational_sns_subscription_endpoints values must be nonempty."
  }
}

variable "enable_dynamodb_point_in_time_recovery" {
  description = "Whether DynamoDB point-in-time recovery is enabled. Chapter 05 leaves the production decision open."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to created resources."
  type        = map(string)
  default = {
    project = "aws-public-change-feed"
  }
}

variable "worker_artifact_sha256" {
  description = "Lowercase SHA-256 digest of the exact published Slack worker package bytes. Null leaves Slice 2 undeployed."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.worker_artifact_sha256 == null || can(regex("^[a-f0-9]{64}$", var.worker_artifact_sha256))
    error_message = "worker_artifact_sha256 must be null or exactly 64 lowercase hexadecimal characters."
  }

  validation {
    condition     = (var.worker_artifact_sha256 == null) == (var.worker_artifact_version_id == null)
    error_message = "worker_artifact_sha256 and worker_artifact_version_id must both be set or both be null."
  }
}

variable "worker_artifact_version_id" {
  description = "Exact S3 VersionId returned by append-only publication of worker_artifact_sha256."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.worker_artifact_version_id == null || length(trimspace(var.worker_artifact_version_id)) > 0
    error_message = "worker_artifact_version_id must be null or a nonempty S3 version ID."
  }
}

variable "watcher_artifact_sha256" {
  description = "Lowercase SHA-256 digest of the exact published feed watcher package bytes. Null leaves Ledger 4 undeployed."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.watcher_artifact_sha256 == null || can(regex("^[a-f0-9]{64}$", var.watcher_artifact_sha256))
    error_message = "watcher_artifact_sha256 must be null or exactly 64 lowercase hexadecimal characters."
  }

  validation {
    condition     = (var.watcher_artifact_sha256 == null) == (var.watcher_artifact_version_id == null)
    error_message = "watcher_artifact_sha256 and watcher_artifact_version_id must both be set or both be null."
  }

  validation {
    condition = var.watcher_artifact_sha256 == null || (
      var.worker_artifact_sha256 != null && var.watcher_artifact_sha256 == var.worker_artifact_sha256
    )
    error_message = "enabling the watcher requires the exact same artifact digest as the enabled Slack worker."
  }
}

variable "watcher_artifact_version_id" {
  description = "Exact S3 VersionId returned by append-only publication of watcher_artifact_sha256."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.watcher_artifact_version_id == null || length(trimspace(var.watcher_artifact_version_id)) > 0
    error_message = "watcher_artifact_version_id must be null or a nonempty S3 version ID."
  }

  validation {
    condition = var.watcher_artifact_version_id == null || (
      var.worker_artifact_version_id != null && var.watcher_artifact_version_id == var.worker_artifact_version_id
    )
    error_message = "enabling the watcher requires the exact same S3 VersionId as the enabled Slack worker."
  }
}

variable "dispatcher_artifact_sha256" {
  description = "Lowercase SHA-256 digest of the exact published outbox dispatcher package bytes. Null leaves Ledger 6 undeployed."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.dispatcher_artifact_sha256 == null || can(regex("^[a-f0-9]{64}$", var.dispatcher_artifact_sha256))
    error_message = "dispatcher_artifact_sha256 must be null or exactly 64 lowercase hexadecimal characters."
  }

  validation {
    condition     = (var.dispatcher_artifact_sha256 == null) == (var.dispatcher_artifact_version_id == null)
    error_message = "dispatcher_artifact_sha256 and dispatcher_artifact_version_id must both be set or both be null."
  }

  validation {
    condition = var.dispatcher_artifact_sha256 == null || (
      var.worker_artifact_sha256 != null && var.dispatcher_artifact_sha256 == var.worker_artifact_sha256
    )
    error_message = "enabling the dispatcher requires the exact same artifact digest as the enabled Slack worker."
  }
}

variable "dispatcher_artifact_version_id" {
  description = "Exact S3 VersionId returned by append-only publication of dispatcher_artifact_sha256."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.dispatcher_artifact_version_id == null || length(trimspace(var.dispatcher_artifact_version_id)) > 0
    error_message = "dispatcher_artifact_version_id must be null or a nonempty S3 version ID."
  }

  validation {
    condition = var.dispatcher_artifact_version_id == null || (
      var.worker_artifact_version_id != null && var.dispatcher_artifact_version_id == var.worker_artifact_version_id
    )
    error_message = "enabling the dispatcher requires the exact same S3 VersionId as the enabled Slack worker."
  }
}

variable "reconciler_artifact_sha256" {
  description = "Lowercase SHA-256 digest of the exact published recovery reconciler package bytes. Null leaves Ledger 3 undeployed."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.reconciler_artifact_sha256 == null || can(regex("^[a-f0-9]{64}$", var.reconciler_artifact_sha256))
    error_message = "reconciler_artifact_sha256 must be null or exactly 64 lowercase hexadecimal characters."
  }

  validation {
    condition     = (var.reconciler_artifact_sha256 == null) == (var.reconciler_artifact_version_id == null)
    error_message = "reconciler_artifact_sha256 and reconciler_artifact_version_id must both be set or both be null."
  }
}

variable "reconciler_artifact_version_id" {
  description = "Exact S3 VersionId returned by append-only publication of reconciler_artifact_sha256."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.reconciler_artifact_version_id == null || length(trimspace(var.reconciler_artifact_version_id)) > 0
    error_message = "reconciler_artifact_version_id must be null or a nonempty S3 version ID."
  }
}

variable "delivery_triggers_enabled" {
  description = "Whether the watcher and dispatcher schedules and Slack worker event-source mapping are enabled after deployment and preflight."
  type        = bool
  default     = false
  nullable    = false

  validation {
    condition = !var.delivery_triggers_enabled || (
      var.worker_artifact_sha256 != null && var.worker_artifact_version_id != null &&
      var.watcher_artifact_sha256 != null && var.watcher_artifact_version_id != null &&
      var.dispatcher_artifact_sha256 != null && var.dispatcher_artifact_version_id != null
    )
    error_message = "delivery_triggers_enabled requires complete worker, watcher, and dispatcher artifact pairs."
  }
}

variable "reconciler_trigger_enabled" {
  description = "Whether the recovery reconciler schedule is enabled after its separate deployment and preflight."
  type        = bool
  default     = false
  nullable    = false

  validation {
    condition = !var.reconciler_trigger_enabled || (
      var.reconciler_artifact_sha256 != null && var.reconciler_artifact_version_id != null
    )
    error_message = "reconciler_trigger_enabled requires a complete reconciler artifact pair."
  }
}
