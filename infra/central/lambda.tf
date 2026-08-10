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
  enabled                            = true

  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = local.rate_control.worker_reserved_concurrency
  }
}
