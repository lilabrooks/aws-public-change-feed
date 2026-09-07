locals {
  tags = {
    project       = "aws-public-change-feed"
    deployment_id = "dev"
    managed_by    = "terraform"
    component     = "live-control"
    purpose       = "bounded-live-control"
  }
}
