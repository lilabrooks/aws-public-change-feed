# Backend blocks cannot read variables. This exact key is also present in the
# bootstrap principal policy and in the exercise runner's refusal contract.
terraform {
  backend "s3" {
    bucket       = "apcf-state-dev"
    key          = "apcf/preflight/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
  }
}
