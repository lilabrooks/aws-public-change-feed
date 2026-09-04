provider "aws" {
  region = local.deployment.deployment_region
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
