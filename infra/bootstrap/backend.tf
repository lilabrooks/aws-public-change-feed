# This root stores its state in the bucket it creates, so the first apply in a
# new deployment runs with this block commented out and a local state file, then
# reinstates it and runs `terraform init -migrate-state`.
#
# Backend blocks cannot read variables, so the bucket name is literal here while
# main.tf derives it from var.deployment_id. The two must be edited together: a
# deployment_id other than "dev" with this block unchanged writes its state into
# the dev deployment's bucket.
terraform {
  backend "s3" {
    bucket       = "apcf-state-dev"
    key          = "apcf/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
  }
}
