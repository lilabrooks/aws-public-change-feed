resource "aws_dynamodb_table" "source_state" {
  name         = local.source_state_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    enabled        = true
    attribute_name = "expires_at"
  }

  point_in_time_recovery {
    enabled                 = var.enable_dynamodb_point_in_time_recovery
    recovery_period_in_days = var.enable_dynamodb_point_in_time_recovery ? var.dynamodb_recovery_period_days : null
  }

  tags = local.tags
}

resource "aws_dynamodb_table" "delivery" {
  name         = local.delivery_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "next_action_at"
    type = "N"
  }

  global_secondary_index {
    name = local.delivery_index_name

    key_schema {
      attribute_name = "status"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "next_action_at"
      key_type       = "RANGE"
    }

    projection_type = "ALL"
  }

  ttl {
    enabled        = true
    attribute_name = "expires_at"
  }

  point_in_time_recovery {
    enabled                 = var.enable_dynamodb_point_in_time_recovery
    recovery_period_in_days = var.enable_dynamodb_point_in_time_recovery ? var.dynamodb_recovery_period_days : null
  }

  tags = local.tags
}

resource "terraform_data" "dynamodb_recovery_cutover_guard" {
  input = var.dynamodb_recovery_cutover

  lifecycle {
    precondition {
      condition = var.dynamodb_recovery_cutover == null || (
        var.enable_dynamodb_point_in_time_recovery &&
        var.watcher_execution_paused &&
        !local.watcher_trigger_requested &&
        !local.dispatcher_trigger_requested &&
        !local.worker_trigger_requested &&
        !var.reconciler_trigger_enabled
      )
      error_message = "ADR-027 recovery cutover requires PITR, all four requested trigger states disabled, and watcher execution paused."
    }

    precondition {
      condition = var.dynamodb_recovery_cutover == null || (
        startswith(var.dynamodb_recovery_cutover.source_state_table, "${local.source_state_table}-restore-") &&
        startswith(var.dynamodb_recovery_cutover.delivery_table, "${local.delivery_table}-restore-") &&
        trimprefix(var.dynamodb_recovery_cutover.source_state_table, "${local.source_state_table}-restore-") == trimprefix(var.dynamodb_recovery_cutover.delivery_table, "${local.delivery_table}-restore-") &&
        var.dynamodb_recovery_cutover.source_state_table != var.dynamodb_recovery_cutover.delivery_table
      )
      error_message = "ADR-027 recovery table names must use the exact primary-table restore prefixes, share one exercise ID, and remain distinct."
    }
  }
}
