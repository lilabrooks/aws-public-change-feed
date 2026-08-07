# Backend blocks cannot read variables, so the bucket name is literal here while
# deployment.yaml carries deployment_id. The two must be edited together.
terraform {
  backend "s3" {
    bucket       = "apcf-state-dev"
    key          = "apcf/central/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
  }
}
