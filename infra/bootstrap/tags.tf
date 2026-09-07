locals {
  tags = merge(var.tags, {
    project       = "aws-public-change-feed"
    deployment_id = var.deployment_id
    managed_by    = "terraform"
    component     = "storage"
  })
}
