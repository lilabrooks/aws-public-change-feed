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
    enabled = var.enable_dynamodb_point_in_time_recovery
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
    name            = "status-next-action-index"
    hash_key        = "status"
    range_key       = "next_action_at"
    projection_type = "ALL"
  }

  ttl {
    enabled        = true
    attribute_name = "expires_at"
  }

  point_in_time_recovery {
    enabled = var.enable_dynamodb_point_in_time_recovery
  }

  tags = local.tags
}
