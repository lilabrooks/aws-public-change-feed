terraform {
  backend "s3" {
    bucket       = "apcf-state-dev"
    key          = "apcf/central/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
  }
}
