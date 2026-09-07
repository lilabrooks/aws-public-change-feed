locals {
  # Identity tags are fixed; caller-supplied supplementary tags are preserved.
  # Components describe ownership, never the current live/parked phase.
  tags = merge(var.tags, {
    project       = "aws-public-change-feed"
    deployment_id = local.deployment_id
    managed_by    = "terraform"
    component     = "runtime"
  })
  storage_tags      = merge(local.tags, { component = "storage" })
  monitoring_tags   = merge(local.tags, { component = "monitoring" })
  common_alarm_tags = local.monitoring_tags
}
