resource "aws_s3_bucket" "config" {
  bucket        = local.config_bucket_name
  force_destroy = false
  tags          = local.tags

  lifecycle {
    precondition {
      condition = var.preflight_mode || (
        var.runtime_artifact_bucket_name == null &&
        var.watcher_trigger_enabled_override == null &&
        var.dispatcher_trigger_enabled_override == null &&
        var.worker_trigger_enabled_override == null
      )
      error_message = "external runtime artifacts and individual trigger overrides are restricted to preflight_mode."
    }

    precondition {
      condition     = !var.preflight_mode || local.deployment_id == "preflight"
      error_message = "preflight_mode requires deployment_id preflight."
    }
  }
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

  # The manifest is the one key that is overwritten in place, so its superseded
  # versions are the retained promotion history ADR-019 relies on. The filter is
  # the exact key: a prefix of the whole tree would silently govern the release
  # and snapshot objects too, and the retention numbers only happen to agree.
  rule {
    id     = "manifest-retention"
    status = "Enabled"

    filter {
      prefix = local.active_versions_key
    }

    noncurrent_version_expiration {
      noncurrent_days           = local.deployment.s3_lifecycle.manifest_noncurrent_version_expiration_days
      newer_noncurrent_versions = local.deployment.s3_lifecycle.minimum_retained_releases
    }
  }

  # Raw snapshots need two rules on a versioned bucket. Expiration alone only
  # writes a delete marker and leaves the body as a noncurrent version, and S3
  # expires a noncurrent version only when both noncurrent_days and
  # newer_noncurrent_versions are exceeded. The second rule reaps the markers
  # once they are the only version left. ExpiredObjectDeleteMarker cannot share
  # a rule with Days, which is why this is not one rule.
  rule {
    id     = "raw-feed-snapshots"
    status = "Enabled"

    filter {
      prefix = local.raw_snapshot_prefix
    }

    expiration {
      days = local.deployment.s3_lifecycle.raw_feed_snapshots_expiration_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  rule {
    id     = "raw-feed-snapshot-delete-markers"
    status = "Enabled"

    filter {
      prefix = local.raw_snapshot_prefix
    }

    expiration {
      expired_object_delete_marker = true
    }
  }
}

# No lifecycle rule governs ${local.release_prefix}/. Release objects are
# write-once at a per-release key, created with If-None-Match: * and never
# overwritten, so they have no noncurrent versions for a noncurrent_version
# rule to reach. The age-based alternative is unsafe: it deletes by object age
# with no notion of which release is active, and a deployment that has not
# republished within retired_release_retention_days would lose the release its
# candidates still resolve against. S3 lifecycle also cannot express the
# minimum_retained_releases floor, which counts releases rather than versions.
# Retiring old releases therefore belongs to the publisher, which knows the
# active pointer and the release order. Tracked in docs/GOAL.md.
