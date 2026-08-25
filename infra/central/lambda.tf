resource "aws_lambda_function" "watcher" {
  count = local.watcher_runtime_enabled ? 1 : 0

  function_name = local.function_names.watcher
  role          = aws_iam_role.feed_watcher.arn
  runtime       = "python3.12"
  architectures = ["x86_64"]
  handler       = "aws_public_change_feed.watcher_runtime.lambda_handler"

  s3_bucket         = aws_s3_bucket.config.id
  s3_key            = local.watcher_artifact_key
  s3_object_version = var.watcher_artifact_version_id

  timeout                        = local.watcher_timeout_seconds
  reserved_concurrent_executions = local.watcher_reserved_concurrency
  memory_size                    = 256

  environment {
    variables = {
      ACTIVE_VERSIONS_OBJECT_KEY    = local.active_versions_key
      APPLICATION_VERSION           = local.watcher_application_version
      APPROVED_FEED_HOSTS_JSON      = jsonencode(local.feed_fetch_policy.allowed_feed_hosts)
      CONFIG_BUCKET                 = aws_s3_bucket.config.id
      DELIVERY_INDEX_NAME           = local.delivery_index_name
      DELIVERY_TABLE_NAME           = aws_dynamodb_table.delivery.name
      FEED_CONNECT_TIMEOUT_SECONDS  = tostring(local.feed_fetch_policy.connect_timeout_seconds)
      FEED_LEASE_SECONDS            = tostring(local.watcher_lease_seconds)
      FEED_RESPONSE_TIMEOUT_SECONDS = tostring(local.feed_fetch_policy.response_timeout_seconds)
      MAX_CONCURRENT_FETCHES        = tostring(local.feed_fetch_policy.max_concurrent_fetches)
      MAX_FEED_ITEM_CHARACTERS      = tostring(local.feed_fetch_policy.max_item_characters)
      MAX_FEED_ITEMS                = tostring(local.feed_fetch_policy.max_items_per_feed)
      MAX_FEED_REDIRECTS            = tostring(local.feed_fetch_policy.max_redirects)
      MAX_FEED_RESPONSE_BYTES       = tostring(local.feed_fetch_policy.max_response_bytes)
      METRICS_NAMESPACE             = local.metrics_namespace
      RAW_SNAPSHOT_PREFIX           = local.raw_snapshot_prefix
      SOURCE_STATE_TABLE_NAME       = aws_dynamodb_table.source_state.name
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.watcher,
    aws_iam_role_policy.feed_watcher,
    aws_iam_role_policy.watcher_logs,
  ]

  tags = local.tags
}

resource "aws_cloudwatch_event_rule" "watcher" {
  count = local.watcher_runtime_enabled ? 1 : 0

  name                = local.function_names.watcher
  description         = "Run the durable public-feed watcher every 15 minutes."
  schedule_expression = local.watcher_schedule_expression
  state               = local.watcher_trigger_enabled ? "ENABLED" : "DISABLED"

  tags = local.tags
}

resource "aws_cloudwatch_event_target" "watcher" {
  count = local.watcher_runtime_enabled ? 1 : 0

  rule = aws_cloudwatch_event_rule.watcher[0].name
  arn  = aws_lambda_function.watcher[0].arn

  retry_policy {
    maximum_event_age_in_seconds = local.watcher_maximum_event_age
    maximum_retry_attempts       = local.watcher_maximum_retry_attempts
  }

  dead_letter_config {
    arn = aws_sqs_queue.runtime_failures.arn
  }

  depends_on = [aws_sqs_queue_policy.runtime_failures]
}

resource "aws_lambda_permission" "watcher_schedule" {
  count = local.watcher_runtime_enabled ? 1 : 0

  statement_id  = "AllowExactWatcherSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.watcher[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.watcher[0].arn
}

resource "aws_lambda_function" "dispatcher" {
  count = local.dispatcher_runtime_enabled ? 1 : 0

  function_name = local.function_names.dispatcher
  role          = aws_iam_role.outbox_dispatcher.arn
  runtime       = "python3.12"
  architectures = ["x86_64"]
  handler       = "aws_public_change_feed.dispatcher_runtime.lambda_handler"

  s3_bucket         = aws_s3_bucket.config.id
  s3_key            = local.dispatcher_artifact_key
  s3_object_version = var.dispatcher_artifact_version_id

  timeout                        = local.dispatcher_timeout_seconds
  reserved_concurrent_executions = local.dispatcher_reserved_concurrency
  memory_size                    = 256

  environment {
    variables = {
      DELIVERY_INDEX_NAME        = local.delivery_index_name
      DELIVERY_QUEUE_URL         = aws_sqs_queue.delivery.id
      DELIVERY_TABLE_NAME        = aws_dynamodb_table.delivery.name
      MAX_DELIVERY_REQUEST_BYTES = tostring(local.max_delivery_request_bytes)
      METRICS_NAMESPACE          = local.metrics_namespace
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.dispatcher,
    aws_iam_role_policy.outbox_dispatcher,
    aws_iam_role_policy.dispatcher_logs,
  ]

  tags = local.tags
}

resource "aws_cloudwatch_event_rule" "dispatcher" {
  count = local.dispatcher_runtime_enabled ? 1 : 0

  name                = local.function_names.dispatcher
  description         = "Run the durable outbox dispatcher every minute."
  schedule_expression = local.dispatcher_schedule_expression
  state               = local.dispatcher_trigger_enabled ? "ENABLED" : "DISABLED"

  tags = local.tags
}

resource "aws_cloudwatch_event_target" "dispatcher" {
  count = local.dispatcher_runtime_enabled ? 1 : 0

  rule = aws_cloudwatch_event_rule.dispatcher[0].name
  arn  = aws_lambda_function.dispatcher[0].arn

  retry_policy {
    maximum_event_age_in_seconds = local.dispatcher_maximum_event_age
    maximum_retry_attempts       = local.dispatcher_maximum_retry_attempts
  }

  dead_letter_config {
    arn = aws_sqs_queue.runtime_failures.arn
  }

  depends_on = [aws_sqs_queue_policy.runtime_failures]
}

resource "aws_lambda_permission" "dispatcher_schedule" {
  count = local.dispatcher_runtime_enabled ? 1 : 0

  statement_id  = "AllowExactDispatcherSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dispatcher[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.dispatcher[0].arn
}

resource "aws_lambda_function" "slack_worker" {
  count = local.worker_runtime_enabled ? 1 : 0

  function_name = local.function_names.worker
  role          = aws_iam_role.slack_worker.arn
  runtime       = "python3.12"
  architectures = ["x86_64"]
  handler       = "aws_public_change_feed.slack_worker_runtime.lambda_handler"

  s3_bucket         = aws_s3_bucket.config.id
  s3_key            = local.worker_artifact_key
  s3_object_version = var.worker_artifact_version_id

  timeout                        = local.worker_timeout_seconds
  reserved_concurrent_executions = local.rate_control.worker_reserved_concurrency
  memory_size                    = 256

  environment {
    variables = {
      APPLICATION_VERSION                = local.application_version
      APPLICATION_ARTIFACT_PREFIX        = local.application_artifact_prefix
      CONFIG_BUCKET_NAME                 = aws_s3_bucket.config.id
      DELIVERY_INDEX_NAME                = local.delivery_index_name
      DELIVERY_MODE                      = local.deployment.slack.delivery_mode
      DELIVERY_TABLE_NAME                = aws_dynamodb_table.delivery.name
      MAX_DELIVERY_REQUEST_BYTES         = tostring(local.max_delivery_request_bytes)
      METRICS_NAMESPACE                  = local.metrics_namespace
      SECRET_STORE                       = local.secret_store
      WORKER_LEASE_DURATION_SECONDS      = tostring(local.worker_lease_duration_seconds)
      WORKER_SAFETY_RESERVE_MILLISECONDS = tostring(local.worker_safety_reserve_milliseconds)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.worker,
    aws_iam_role_policy.slack_worker,
    aws_iam_role_policy.worker_logs,
  ]

  tags = local.tags
}

resource "aws_lambda_event_source_mapping" "slack_worker" {
  count = local.worker_runtime_enabled ? 1 : 0

  event_source_arn                   = aws_sqs_queue.delivery.arn
  function_name                      = aws_lambda_function.slack_worker[0].arn
  batch_size                         = local.worker_batch_size
  maximum_batching_window_in_seconds = local.worker_batch_window_seconds
  enabled                            = local.worker_trigger_enabled

  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = local.rate_control.worker_reserved_concurrency
  }
}

resource "aws_lambda_function" "reconciler" {
  count = local.reconciler_runtime_enabled ? 1 : 0

  function_name = local.function_names.reconciler
  role          = aws_iam_role.recovery_reconciler.arn
  runtime       = "python3.12"
  architectures = ["x86_64"]
  handler       = "aws_public_change_feed.recovery_runtime.lambda_handler"

  s3_bucket         = aws_s3_bucket.config.id
  s3_key            = local.reconciler_artifact_key
  s3_object_version = var.reconciler_artifact_version_id

  timeout                        = local.reconciler_timeout_seconds
  reserved_concurrent_executions = local.reconciler_reserved_concurrency
  memory_size                    = 256

  environment {
    variables = {
      DELIVERY_INDEX_NAME           = local.delivery_index_name
      DELIVERY_QUEUE_URL            = aws_sqs_queue.delivery.id
      DELIVERY_TABLE_NAME           = aws_dynamodb_table.delivery.name
      MAX_DELIVERY_REQUEST_BYTES    = tostring(local.max_delivery_request_bytes)
      METRICS_NAMESPACE             = local.metrics_namespace
      RECOVERY_OBSERVATION_LIMIT    = tostring(local.reconciler_observation_limit)
      RECOVERY_REPAIR_LIMIT         = tostring(local.reconciler_repair_limit)
      RECOVERY_STALE_QUEUED_SECONDS = tostring(local.reconciler_stale_queued_seconds)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.reconciler,
    aws_iam_role_policy.recovery_reconciler,
    aws_iam_role_policy.reconciler_logs,
  ]

  tags = local.tags
}

resource "aws_cloudwatch_event_rule" "reconciler" {
  name                = local.function_names.reconciler
  description         = "Run bounded delivery recovery every five minutes."
  schedule_expression = local.reconciler_schedule_expression
  state               = local.reconciler_trigger_enabled ? "ENABLED" : "DISABLED"

  tags = local.tags
}

resource "aws_cloudwatch_event_target" "reconciler" {
  count = local.reconciler_runtime_enabled ? 1 : 0

  rule = aws_cloudwatch_event_rule.reconciler.name
  arn  = aws_lambda_function.reconciler[0].arn

  retry_policy {
    maximum_event_age_in_seconds = local.reconciler_maximum_event_age
    maximum_retry_attempts       = local.reconciler_maximum_retry_attempts
  }

  dead_letter_config {
    arn = aws_sqs_queue.runtime_failures.arn
  }

  depends_on = [aws_sqs_queue_policy.runtime_failures]
}

resource "aws_lambda_permission" "reconciler_schedule" {
  count = local.reconciler_runtime_enabled ? 1 : 0

  statement_id  = "AllowExactReconcilerSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reconciler[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reconciler.arn
}
