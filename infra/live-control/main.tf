terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0.0, < 7.0.0"
    }
  }
  backend "s3" {
    bucket       = "apcf-state-dev"
    key          = "apcf/live-control/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
  }
}

provider "aws" {
  region              = "us-east-1"
  allowed_account_ids = ["667653114001"]
}

locals {
  timing  = jsondecode(file("${path.module}/../../scripts/live_window_budget.json"))
  prefix  = "apcf-dev-live-control"
  account = "667653114001"
  region  = "us-east-1"
}

resource "aws_s3_bucket" "control" {
  bucket        = "${local.prefix}-${local.account}"
  force_destroy = false
  tags          = local.tags
}

resource "aws_s3_bucket_versioning" "control" {
  bucket = aws_s3_bucket.control.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Completed sources/evidence require state-aware retirement, never an age-only
# expiration rule. This rule reclaims only unfinished multipart uploads.
resource "aws_s3_bucket_lifecycle_configuration" "control" {
  bucket = aws_s3_bucket.control.id
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "control" {
  bucket = aws_s3_bucket.control.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "control" {
  bucket                  = aws_s3_bucket.control.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "control" {
  bucket = aws_s3_bucket.control.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Deny", Principal = "*", Action = "s3:*",
      Resource  = [aws_s3_bucket.control.arn, "${aws_s3_bucket.control.arn}/*"],
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# No TTL: a failed or abandoned operation must never silently lose ownership.
resource "aws_dynamodb_table" "control" {
  name                        = local.prefix
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "id"
  deletion_protection_enabled = true
  attribute {
    name = "id"
    type = "S"
  }
  tags = local.tags
}

resource "aws_cloudwatch_log_group" "build" {
  name              = "/aws/codebuild/${local.prefix}"
  retention_in_days = 14
  tags              = local.tags
}

resource "aws_codebuild_project" "control" {
  name                   = local.prefix
  service_role           = aws_iam_role.build.arn
  build_timeout          = local.timing.build_timeout_minutes
  queued_timeout         = local.timing.queued_timeout_minutes
  concurrent_build_limit = 1
  artifacts {
    type = "NO_ARTIFACTS"
  }
  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/standard:7.0"
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "CODEBUILD"
    privileged_mode             = false
    environment_variable {
      name  = "LIVE_CONTROL_TABLE"
      value = aws_dynamodb_table.control.name
    }
    environment_variable {
      name  = "LIVE_CONTROL_BUCKET"
      value = aws_s3_bucket.control.id
    }
  }
  source {
    type      = "S3"
    location  = "${aws_s3_bucket.control.id}/unconfigured.zip"
    buildspec = "infra/live-control/buildspec.yml"
  }
  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.build.name
    }
  }
  tags = local.tags
}

output "live_control" {
  description = "One-time operator configuration; contains no credentials."
  value = {
    account       = local.account
    region        = local.region
    bucket        = aws_s3_bucket.control.id
    table         = aws_dynamodb_table.control.name
    project       = aws_codebuild_project.control.name
    state_machine = aws_sfn_state_machine.control.arn
  }
}
