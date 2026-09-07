locals {
  operations_topic_arn = "arn:aws:sns:${local.region}:${local.account}:${yamldecode(file("${path.module}/../central/deployment.yaml")).operational_sns_topic_name}"
  failure_events = {
    workflow = {
      source      = ["aws.states"]
      detail-type = ["Step Functions Execution Status Change"]
      detail = {
        stateMachineArn = [aws_sfn_state_machine.control.arn]
        status          = ["FAILED", "TIMED_OUT", "ABORTED"]
      }
    }
    build = {
      source      = ["aws.codebuild"]
      detail-type = ["CodeBuild Build State Change"]
      detail = {
        project-name = [aws_codebuild_project.control.name]
        build-status = ["FAILED", "FAULT", "TIMED_OUT", "STOPPED"]
      }
    }
  }
}

# Event patterns, not schedules or metered CloudWatch metric alarms. These remain
# available while central is parked; central alone owns the SNS topic policy.
resource "aws_cloudwatch_event_rule" "failure" {
  for_each      = local.failure_events
  name          = "${local.prefix}-${each.key}-failure"
  event_pattern = jsonencode(each.value)
  tags          = local.tags
}

resource "aws_iam_role" "failure_events" {
  name = "${local.prefix}-failure-events"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { Service = "events.amazonaws.com" }, Action = "sts:AssumeRole",
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account },
        ArnEquals    = { "aws:SourceArn" = [for rule in aws_cloudwatch_event_rule.failure : rule.arn] }
      }
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy" "failure_events" {
  role = aws_iam_role.failure_events.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["sns:Publish"], Resource = local.operations_topic_arn }]
  })
}

resource "aws_cloudwatch_event_target" "failure" {
  for_each  = local.failure_events
  rule      = aws_cloudwatch_event_rule.failure[each.key].name
  target_id = "operations-email"
  arn       = local.operations_topic_arn
  role_arn  = aws_iam_role.failure_events.arn
  input_transformer {
    input_paths = {
      id     = each.key == "workflow" ? "$.detail.executionArn" : "$.detail.build-id"
      status = each.key == "workflow" ? "$.detail.status" : "$.detail.build-status"
    }
    input_template = "\"APCF live-control ${each.key}: <status>. ID: <id>. Parking status needs checking; a build failure may still recover. Run live-status. Console: https://${local.region}.console.aws.amazon.com/${each.key == "workflow" ? "states" : "codesuite/codebuild"}/home?region=${local.region}\""
  }
  depends_on = [aws_iam_role_policy.failure_events]
}

resource "aws_iam_role_policy" "cleanup_notification" {
  role = aws_iam_role.workflow.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["sns:Publish"], Resource = local.operations_topic_arn }]
  })
}
