locals {
  bucket_name         = "apcf-state-${var.deployment_id}"
  bootstrap_state_key = "apcf/terraform.tfstate"
  central_state_key   = "apcf/central/terraform.tfstate"
  backend_state_keys  = [local.bootstrap_state_key, local.central_state_key]
}

resource "aws_s3_bucket" "state" {
  bucket        = local.bucket_name
  force_destroy = false
  tags          = var.tags
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "state_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.state_bucket.json
}

data "aws_iam_policy_document" "backend_principal" {
  statement {
    sid = "StateObjectActions"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]

    resources = [
      for key in local.backend_state_keys : "${aws_s3_bucket.state.arn}/${key}"
    ]
  }

  statement {
    sid = "LockfileObjectActions"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]

    resources = [
      for key in local.backend_state_keys : "${aws_s3_bucket.state.arn}/${key}.tflock"
    ]
  }

  statement {
    sid = "StateListBucket"

    actions = [
      "s3:ListBucket",
    ]

    resources = [
      aws_s3_bucket.state.arn,
    ]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = local.backend_state_keys
    }
  }
}
