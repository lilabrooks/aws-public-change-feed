resource "aws_cloudwatch_log_group" "watcher" {
  name              = "/aws/lambda/${local.function_names.watcher}"
  retention_in_days = local.log_retention_days
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "shadow" {
  name              = "/aws/lambda/${local.function_names.shadow}"
  retention_in_days = local.log_retention_days
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "dispatcher" {
  name              = "/aws/lambda/${local.function_names.dispatcher}"
  retention_in_days = local.log_retention_days
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${local.function_names.worker}"
  retention_in_days = local.log_retention_days
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "reconciler" {
  name              = "/aws/lambda/${local.function_names.reconciler}"
  retention_in_days = local.log_retention_days
  tags              = local.tags
}
