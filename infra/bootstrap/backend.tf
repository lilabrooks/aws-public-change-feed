terraform {
  backend "s3" {
    bucket       = "apcf-state-dev"
    key          = "apcf/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
  }
}
