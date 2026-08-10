data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "publisher_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

resource "aws_iam_role" "release_publisher" {
  name               = local.role_names.publisher
  assume_role_policy = data.aws_iam_policy_document.publisher_assume_role.json
  tags               = local.tags
}

resource "aws_iam_role" "feed_watcher" {
  name               = local.role_names.watcher
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.tags
}

resource "aws_iam_role" "outbox_dispatcher" {
  name               = local.role_names.dispatcher
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.tags
}

resource "aws_iam_role" "slack_worker" {
  name               = local.role_names.worker
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.tags
}

resource "aws_iam_role" "recovery_reconciler" {
  name               = local.role_names.reconciler
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.tags
}

data "aws_iam_policy_document" "release_publisher" {
  statement {
    sid       = "ReadActiveManifest"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.config.arn}/${local.active_versions_key}"]
  }

  statement {
    sid     = "ReadExactVersions"
    actions = ["s3:GetObjectVersion"]
    resources = [
      "${aws_s3_bucket.config.arn}/${local.active_versions_key}",
      "${aws_s3_bucket.config.arn}/${local.release_prefix}/*",
    ]
  }

  statement {
    sid     = "PublishReleasesAndManifest"
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.config.arn}/${local.active_versions_key}",
      "${aws_s3_bucket.config.arn}/${local.release_prefix}/*",
    ]
  }

  statement {
    sid     = "PublishApplicationArtifacts"
    actions = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
    resources = [
      "${aws_s3_bucket.config.arn}/${local.application_artifact_prefix}/*",
    ]
  }
}

data "aws_iam_policy_document" "feed_watcher" {
  statement {
    sid       = "ReadActiveManifest"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.config.arn}/${local.active_versions_key}"]
  }

  statement {
    sid       = "ReadReleaseVersions"
    actions   = ["s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.config.arn}/${local.release_prefix}/*"]
  }

  statement {
    sid       = "WriteRawSnapshots"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.config.arn}/${local.raw_snapshot_prefix}*"]
  }

  statement {
    sid     = "SourceState"
    actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:TransactWriteItems"]
    resources = [
      aws_dynamodb_table.source_state.arn,
    ]
  }

  # emit() reads the stored candidate and delivery record before writing, so the
  # watcher needs GetItem on the delivery table and not writes alone.
  statement {
    sid     = "DurableOutboxWrites"
    actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [
      aws_dynamodb_table.delivery.arn,
    ]
  }
}

data "aws_iam_policy_document" "outbox_dispatcher" {
  # A global secondary index is a distinct IAM resource. Querying
  # status-next-action-index needs the index ARN; the table ARN alone denies it.
  statement {
    sid     = "QueryDeliveryIndex"
    actions = ["dynamodb:Query"]
    resources = [
      aws_dynamodb_table.delivery.arn,
      local.delivery_index_arn,
    ]
  }

  statement {
    sid     = "ReadDeliveryRecords"
    actions = ["dynamodb:GetItem"]
    resources = [
      aws_dynamodb_table.delivery.arn,
    ]
  }

  statement {
    sid     = "ClaimAndTransition"
    actions = ["dynamodb:UpdateItem", "dynamodb:PutItem"]
    resources = [
      aws_dynamodb_table.delivery.arn,
    ]
  }

  statement {
    sid       = "SendQueueMessages"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.delivery.arn]
  }
}

data "aws_iam_policy_document" "slack_worker" {
  statement {
    sid     = "ConsumeQueue"
    actions = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"]
    resources = [
      aws_sqs_queue.delivery.arn,
    ]
  }

  statement {
    sid       = "ReadReleaseVersions"
    actions   = ["s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.config.arn}/${local.release_prefix}/*"]
  }

  statement {
    sid       = "CheckApplicationArtifactAvailability"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.config.arn}/${local.application_artifact_prefix}/*"]
  }

  statement {
    sid     = "DeliveryAndPacingState"
    actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem"]
    resources = [
      aws_dynamodb_table.delivery.arn,
    ]
  }

  statement {
    sid       = "ReadSlackCredentials"
    actions   = local.secret_read_actions
    resources = local.secret_arns
  }
}

data "aws_iam_policy_document" "recovery_reconciler" {
  statement {
    sid     = "QueryDeliveryIndex"
    actions = ["dynamodb:Query"]
    resources = [
      aws_dynamodb_table.delivery.arn,
      local.delivery_index_arn,
    ]
  }

  statement {
    sid     = "ReadDeliveryRecords"
    actions = ["dynamodb:GetItem"]
    resources = [
      aws_dynamodb_table.delivery.arn,
    ]
  }

  statement {
    sid     = "RepairDeliveryState"
    actions = ["dynamodb:UpdateItem"]
    resources = [
      aws_dynamodb_table.delivery.arn,
    ]
  }

  statement {
    sid       = "SendQueueMessages"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.delivery.arn]
  }
}

resource "aws_iam_role_policy" "release_publisher" {
  name   = "release-publisher"
  role   = aws_iam_role.release_publisher.id
  policy = data.aws_iam_policy_document.release_publisher.json
}

resource "aws_iam_role_policy" "feed_watcher" {
  name   = "feed-watcher"
  role   = aws_iam_role.feed_watcher.id
  policy = data.aws_iam_policy_document.feed_watcher.json
}

resource "aws_iam_role_policy" "outbox_dispatcher" {
  name   = "outbox-dispatcher"
  role   = aws_iam_role.outbox_dispatcher.id
  policy = data.aws_iam_policy_document.outbox_dispatcher.json
}

resource "aws_iam_role_policy" "slack_worker" {
  name   = "slack-worker"
  role   = aws_iam_role.slack_worker.id
  policy = data.aws_iam_policy_document.slack_worker.json
}

resource "aws_iam_role_policy" "recovery_reconciler" {
  name   = "recovery-reconciler"
  role   = aws_iam_role.recovery_reconciler.id
  policy = data.aws_iam_policy_document.recovery_reconciler.json
}

resource "aws_iam_role_policy" "watcher_logs" {
  name = "cloudwatch-logs"
  role = aws_iam_role.feed_watcher.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "WriteLogs"
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = ["${aws_cloudwatch_log_group.watcher.arn}:*"]
    }]
  })
}

resource "aws_iam_role_policy" "dispatcher_logs" {
  name = "cloudwatch-logs"
  role = aws_iam_role.outbox_dispatcher.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "WriteLogs"
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = ["${aws_cloudwatch_log_group.dispatcher.arn}:*"]
    }]
  })
}

resource "aws_iam_role_policy" "worker_logs" {
  name = "cloudwatch-logs"
  role = aws_iam_role.slack_worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "WriteLogs"
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = ["${aws_cloudwatch_log_group.worker.arn}:*"]
    }]
  })
}

resource "aws_iam_role_policy" "reconciler_logs" {
  name = "cloudwatch-logs"
  role = aws_iam_role.recovery_reconciler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "WriteLogs"
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = ["${aws_cloudwatch_log_group.reconciler.arn}:*"]
    }]
  })
}
