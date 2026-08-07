resource "aws_sns_topic" "operations" {
  name = local.sns_topic_name
  tags = local.tags
}

data "aws_iam_policy_document" "operations_topic" {
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
