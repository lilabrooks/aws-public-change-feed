# Dedicated bucket for the ADR-019 concurrent-promotion suite (revision
# accepted 2026-08-07). Each suite run writes under a unique key prefix under
# concurrency/, and the lifecycle rules below expire those prefixes, so cleanup
# is declarative rather than code the suite has to get right. The alternative,
# a suite creating and destroying its own bucket, was rejected in ADR-019 on
# failure mode: deleting a versioned bucket requires enumerating every version
# and delete marker, and teardown that half-fails leaves orphaned versioned
# buckets against the account limit.
locals {
  concurrency_bucket_name    = "apcf-concurrency-${var.deployment_id}"
  concurrency_prefix         = "concurrency/"
  concurrency_retention_days = 7
}

resource "aws_s3_bucket" "concurrency" {
  bucket        = local.concurrency_bucket_name
  force_destroy = false
  tags          = var.tags
}

resource "aws_s3_bucket_versioning" "concurrency" {
  bucket = aws_s3_bucket.concurrency.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "concurrency" {
  bucket = aws_s3_bucket.concurrency.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "concurrency" {
  bucket                  = aws_s3_bucket.concurrency.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "concurrency_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.concurrency.arn, "${aws_s3_bucket.concurrency.arn}/*"]

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

resource "aws_s3_bucket_policy" "concurrency" {
  bucket = aws_s3_bucket.concurrency.id
  policy = data.aws_iam_policy_document.concurrency_bucket.json
}

# Two rules on a versioned bucket, the shape the data-plane audit corrected.
# Expiration alone only writes a delete marker and leaves the body as a
# noncurrent version, and S3 expires a noncurrent version only when both
# noncurrent_days and newer_noncurrent_versions are exceeded, so a lone version
# needs a noncurrent_version_expiration without a newer-versions floor. The
# second rule reaps the delete markers once they are the only version left;
# ExpiredObjectDeleteMarker cannot share a rule with Days. This exercises the
# same lifecycle mechanism the outstanding release-retention item needs, which
# is why ADR-019's revision chose it.
resource "aws_s3_bucket_lifecycle_configuration" "concurrency" {
  bucket = aws_s3_bucket.concurrency.id

  rule {
    id     = "concurrency-test-prefixes"
    status = "Enabled"

    filter {
      prefix = local.concurrency_prefix
    }

    expiration {
      days = local.concurrency_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  rule {
    id     = "concurrency-test-delete-markers"
    status = "Enabled"

    filter {
      prefix = local.concurrency_prefix
    }

    expiration {
      expired_object_delete_marker = true
    }
  }
}

# Scoped identity for the operator-run suite: object-level actions on this one
# bucket only. Access keys are never created here or committed; the operator
# runs `aws iam create-access-key --user-name apcf_concurrency_test` when the
# suite needs to run, and the ADR's "credentials enter the testing story for
# the first time" applies to those keys, not to this repository.
resource "aws_iam_user" "concurrency_test" {
  name = "apcf_concurrency_test"
  tags = var.tags
}

data "aws_iam_policy_document" "concurrency_test" {
  statement {
    sid = "ConcurrencyBucketObjectActions"

    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:DeleteObject",
    ]

    resources = [
      aws_s3_bucket.concurrency.arn,
      "${aws_s3_bucket.concurrency.arn}/*",
    ]
  }

  statement {
    sid = "ConcurrencyBucketListVersions"

    actions = [
      "s3:ListBucketVersions",
    ]

    resources = [
      aws_s3_bucket.concurrency.arn,
    ]
  }
}

resource "aws_iam_user_policy" "concurrency_test" {
  name   = "concurrency-test"
  user   = aws_iam_user.concurrency_test.name
  policy = data.aws_iam_policy_document.concurrency_test.json
}
