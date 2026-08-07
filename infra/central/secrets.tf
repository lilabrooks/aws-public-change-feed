resource "aws_secretsmanager_secret" "slack_credentials" {
  count = local.secret_store == "secrets_manager" ? length(local.slack_secret_ids) : 0

  name = local.slack_secret_ids[count.index]
  tags = local.tags
}

resource "aws_ssm_parameter" "slack_credentials" {
  count = local.secret_store == "ssm_parameter_store" ? length(local.slack_secret_ids) : 0

  name  = local.slack_secret_ids[count.index]
  type  = "SecureString"
  value = "placeholder-set-by-operator"

  lifecycle {
    ignore_changes = [value]
  }

  tags = local.tags
}
