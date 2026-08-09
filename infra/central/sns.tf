resource "aws_sns_topic" "operations" {
  name = local.sns_topic_name
  tags = local.tags

  # deploy_operational_sns_topic is const true in deployment.schema.json, so a
  # count on it would be unreachable code. This binds the input to the resource
  # instead: a hand-edited deployment.yaml that turns it off fails the plan
  # rather than quietly getting a topic it asked not to have.
  lifecycle {
    precondition {
      condition     = local.deployment.deploy_operational_sns_topic
      error_message = "deploy_operational_sns_topic must be true; this root always creates the operational topic."
    }
  }
}

data "aws_iam_policy_document" "operations_topic" {
  # CloudWatch alarms publish as the CloudWatch service principal. An
  # account-root grant does not authorize that service call, so keep the
  # principal and its source conditions explicit.
  statement {
    sid       = "AllowCloudWatchAlarmPublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.operations.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:cloudwatch:${local.region}:${data.aws_caller_identity.current.account_id}:alarm:apcf-${local.deployment_id}-*"]
    }
  }

  statement {
    sid       = "AllowAccountPublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.operations.arn]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

resource "aws_sns_topic_policy" "operations" {
  arn    = aws_sns_topic.operations.arn
  policy = data.aws_iam_policy_document.operations_topic.json
}
