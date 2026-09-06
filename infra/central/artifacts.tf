data "aws_s3_object" "shared_runtime_artifact" {
  count = local.worker_runtime_enabled ? 1 : 0

  bucket        = local.runtime_artifact_bucket_name
  key           = local.worker_artifact_key
  version_id    = var.worker_artifact_version_id
  checksum_mode = var.worker_artifact_checksum_sha256 == null ? null : "ENABLED"
}

resource "terraform_data" "shared_runtime_artifact_guard" {
  count = local.worker_runtime_enabled ? 1 : 0

  input = {
    bucket                     = local.runtime_artifact_bucket_name
    key                        = local.worker_artifact_key
    version_id                 = var.worker_artifact_version_id
    sha256                     = var.worker_artifact_sha256
    runtime_entrypoints_sha256 = local.runtime_entrypoints_sha256
    checksum_sha256            = var.worker_artifact_checksum_sha256
  }

  lifecycle {
    precondition {
      condition     = data.aws_s3_object.shared_runtime_artifact[0].version_id == var.worker_artifact_version_id
      error_message = "the shared runtime artifact read-back must return the exact selected S3 VersionId."
    }

    precondition {
      condition     = lookup(data.aws_s3_object.shared_runtime_artifact[0].metadata, "sha256", null) == var.worker_artifact_sha256
      error_message = "the shared runtime artifact metadata must match the selected SHA-256 digest."
    }

    precondition {
      condition     = lookup(data.aws_s3_object.shared_runtime_artifact[0].metadata, "runtime-entrypoints-sha256", null) == local.runtime_entrypoints_sha256
      error_message = "the shared runtime artifact must carry the complete configured-handler contract."
    }

    precondition {
      condition = var.worker_artifact_checksum_sha256 == null || (
        data.aws_s3_object.shared_runtime_artifact[0].checksum_sha256 == var.worker_artifact_checksum_sha256
      )
      error_message = "the shared runtime artifact S3 SHA-256 checksum must match the selected package digest encoding."
    }
  }
}

data "aws_s3_object" "reconciler_runtime_artifact" {
  count = local.reconciler_runtime_enabled ? 1 : 0

  bucket        = local.runtime_artifact_bucket_name
  key           = local.reconciler_artifact_key
  version_id    = var.reconciler_artifact_version_id
  checksum_mode = var.reconciler_artifact_checksum_sha256 == null ? null : "ENABLED"
}

resource "terraform_data" "reconciler_runtime_artifact_guard" {
  count = local.reconciler_runtime_enabled ? 1 : 0

  input = {
    bucket                     = local.runtime_artifact_bucket_name
    key                        = local.reconciler_artifact_key
    version_id                 = var.reconciler_artifact_version_id
    sha256                     = var.reconciler_artifact_sha256
    runtime_entrypoints_sha256 = local.runtime_entrypoints_sha256
    checksum_sha256            = var.reconciler_artifact_checksum_sha256
  }

  lifecycle {
    precondition {
      condition     = data.aws_s3_object.reconciler_runtime_artifact[0].version_id == var.reconciler_artifact_version_id
      error_message = "the reconciler runtime artifact read-back must return the exact selected S3 VersionId."
    }

    precondition {
      condition     = lookup(data.aws_s3_object.reconciler_runtime_artifact[0].metadata, "sha256", null) == var.reconciler_artifact_sha256
      error_message = "the reconciler runtime artifact metadata must match the selected SHA-256 digest."
    }

    precondition {
      condition     = lookup(data.aws_s3_object.reconciler_runtime_artifact[0].metadata, "runtime-entrypoints-sha256", null) == local.runtime_entrypoints_sha256
      error_message = "the reconciler runtime artifact must carry the complete configured-handler contract."
    }

    precondition {
      condition = var.reconciler_artifact_checksum_sha256 == null || (
        data.aws_s3_object.reconciler_runtime_artifact[0].checksum_sha256 == var.reconciler_artifact_checksum_sha256
      )
      error_message = "the reconciler runtime artifact S3 SHA-256 checksum must match the selected package digest encoding."
    }
  }
}
