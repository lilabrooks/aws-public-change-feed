resource "aws_s3_bucket" "config" {
  bucket        = local.config_bucket_name
  force_destroy = false
  tags          = local.tags
}

resource "aws_s3_bucket_versioning" "config" {
  bucket = aws_s3_bucket.config.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "config" {
  bucket = aws_s3_bucket.config.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "config" {
  bucket                  = aws_s3_bucket.config.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "config_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.config.arn, "${aws_s3_bucket.config.arn}/*"]

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

resource "aws_s3_bucket_policy" "config" {
  bucket = aws_s3_bucket.config.id
  policy = data.aws_iam_policy_document.config_bucket.json
}

resource "aws_s3_bucket_lifecycle_configuration" "config" {
  bucket = aws_s3_bucket.config.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    abort_incomplete_multipart_upload {
      days_after_initiation = local.deployment.s3_lifecycle.abort_incomplete_multipart_upload_days
    }
  }

  rule {
    id     = "manifest-retention"
    status = "Enabled"

    filter {
      prefix = "${local.top_prefix}/"
    }

    noncurrent_version_expiration {
      noncurrent_days           = local.deployment.s3_lifecycle.manifest_noncurrent_version_expiration_days
      newer_noncurrent_versions = local.deployment.s3_lifecycle.minimum_retained_releases
    }
  }

  rule {
    id     = "retired-release-retention"
    status = "Enabled"

    filter {
      prefix = "${local.release_prefix}/"
    }

    noncurrent_version_expiration {
      noncurrent_days           = local.deployment.s3_lifecycle.retired_release_retention_days
      newer_noncurrent_versions = local.deployment.s3_lifecycle.minimum_retained_releases
    }
  }

  rule {
    id     = "raw-feed-snapshots"
    status = "Enabled"

    filter {
      prefix = local.raw_snapshot_prefix
    }

    expiration {
      days = local.deployment.s3_lifecycle.raw_feed_snapshots_expiration_days
    }
  }
}
