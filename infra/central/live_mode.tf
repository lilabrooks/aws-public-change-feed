variable "live_mode" {
  description = "Bounded live controller phase. Null preserves legacy rollout/recovery controls; set parked in the complete private deployment inputs after migration."
  type        = string
  default     = null

  validation {
    condition     = var.live_mode == null ? true : contains(["parked", "prepared", "live", "direct", "draining", "stopping", "shadow"], var.live_mode)
    error_message = "live_mode must be null, parked, prepared, live, direct, draining, stopping, or shadow."
  }
}

locals {
  live_managed       = var.live_mode != null
  monitoring_enabled = var.live_mode != "parked"
  live_consumers     = contains(["live", "draining"], coalesce(var.live_mode, "legacy"))
  live_fenced        = contains(["parked", "prepared", "stopping", "shadow"], coalesce(var.live_mode, "legacy"))
}

output "live_mode" {
  description = "Selected lifecycle phase; null denotes legacy operator controls."
  value       = var.live_mode
}

output "runtime_reserved_concurrency" {
  description = "Selected execution fences, including the unscheduled shadow evaluator."
  value = {
    watcher    = local.watcher_reserved_concurrency
    dispatcher = local.dispatcher_reserved_concurrency
    reconciler = local.reconciler_reserved_concurrency
    worker     = local.worker_reserved_concurrency
    shadow     = local.shadow_reserved_concurrency
  }
}
