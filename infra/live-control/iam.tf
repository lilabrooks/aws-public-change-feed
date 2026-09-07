resource "aws_iam_role" "build" {
  name = "${local.prefix}-build"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole",
    Condition = { StringEquals = { "aws:SourceAccount" = local.account } }
  }] })
  tags = local.tags
}

resource "aws_iam_role_policy" "build" {
  role = aws_iam_role.build.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid = "ProviderRefresh", Effect = "Allow", Resource = "*",
        Action = [
          "sts:GetCallerIdentity", "iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
          "iam:ListRoleTags", "iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListInstanceProfilesForRole",
          "s3:GetBucket*", "s3:GetEncryptionConfiguration", "s3:GetLifecycleConfiguration", "s3:GetAccelerateConfiguration", "s3:ListBucket",
          "dynamodb:Describe*", "dynamodb:ListTagsOfResource", "sqs:GetQueueAttributes", "sqs:GetQueueUrl", "sqs:ListQueueTags",
          "lambda:GetFunction*", "lambda:GetPolicy", "lambda:GetRuntimeManagementConfig", "lambda:ListTags", "lambda:ListVersionsByFunction",
          "lambda:GetEventSourceMapping", "lambda:ListEventSourceMappings", "lambda:ListProvisionedConcurrencyConfigs",
          "events:DescribeRule", "events:ListTargetsByRule", "events:ListTagsForResource",
          "cloudwatch:DescribeAlarms", "cloudwatch:DescribeAlarmHistory", "cloudwatch:GetDashboard", "cloudwatch:ListTagsForResource",
          "logs:DescribeLogGroups", "logs:ListTagsForResource", "sns:GetTopicAttributes", "sns:GetSubscriptionAttributes", "sns:ListTagsForResource",
          "secretsmanager:DescribeSecret", "secretsmanager:GetResourcePolicy", "secretsmanager:ListSecretVersionIds"
        ]
      },
      {
        # aws_s3_bucket refresh reads replication even when none is configured.
        Sid    = "RefreshConfigReplication", Effect = "Allow",
        Action = ["s3:GetReplicationConfiguration"], Resource = ["arn:aws:s3:::apcf-config-dev"]
      },
      {
        Sid      = "ReadExactDeploymentObjects", Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"],
        Resource = ["arn:aws:s3:::apcf-config-dev/apcf/*", "${aws_s3_bucket.control.arn}/bundles/*"]
      },
      {
        Sid      = "CentralStateOnly", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"],
        Resource = ["arn:aws:s3:::apcf-state-dev/apcf/central/terraform.tfstate", "arn:aws:s3:::apcf-state-dev/apcf/central/terraform.tfstate.tflock"]
      },
      {
        Sid      = "CentralLockOnly", Effect = "Allow", Action = ["s3:DeleteObject"],
        Resource = ["arn:aws:s3:::apcf-state-dev/apcf/central/terraform.tfstate.tflock"]
      },
      {
        Sid      = "ExecutionFences", Effect = "Allow", Action = ["lambda:PutFunctionConcurrency"],
        Resource = [for name in ["feed-watcher", "outbox-dispatcher", "recovery-reconciler", "slack-worker", "shadow-evaluator"] : "arn:aws:lambda:us-east-1:${local.account}:function:apcf-dev-${name}"]
      },
      {
        Sid      = "ExactQueueTrigger", Effect = "Allow", Action = ["lambda:UpdateEventSourceMapping"],
        Resource = ["arn:aws:lambda:us-east-1:${local.account}:event-source-mapping:245de870-e031-4d39-a5fe-bedc3f0c90f2"]
      },
      {
        Sid      = "ExactSchedules", Effect = "Allow", Action = ["events:PutRule", "events:EnableRule", "events:DisableRule"],
        Resource = [for name in ["feed-watcher", "outbox-dispatcher", "recovery-reconciler"] : "arn:aws:events:us-east-1:${local.account}:rule/apcf-dev-${name}"]
      },
      {
        Sid      = "TemporaryMonitoring", Effect = "Allow",
        Action   = ["cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:TagResource", "cloudwatch:UntagResource", "cloudwatch:PutDashboard", "cloudwatch:DeleteDashboards"],
        Resource = ["arn:aws:cloudwatch:us-east-1:${local.account}:alarm:apcf-dev-*", "arn:aws:cloudwatch::${local.account}:dashboard/apcf-dev-operations"]
      },
      {
        Sid      = "RetainedWorkInventory", Effect = "Allow", Action = ["dynamodb:Scan", "dynamodb:GetItem", "dynamodb:Query"],
        Resource = ["arn:aws:dynamodb:us-east-1:${local.account}:table/apcf-delivery-dev"]
      },
      {
        Sid      = "BoundedDeliveryPreflight", Effect = "Allow", Action = ["lambda:InvokeFunction"],
        Resource = [for name in ["feed-watcher", "outbox-dispatcher", "slack-worker"] : "arn:aws:lambda:us-east-1:${local.account}:function:apcf-dev-${name}"]
      },
      {
        Sid      = "BoundedPreflightTransport", Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage"],
        Resource = ["arn:aws:sqs:us-east-1:${local.account}:apcf-delivery-dev.fifo"]
      },
      {
        Sid      = "ReadDeliveryIndex", Effect = "Allow", Action = ["dynamodb:Query"],
        Resource = ["arn:aws:dynamodb:us-east-1:${local.account}:table/apcf-delivery-dev/index/status-next-action-index"]
      },
      {
        Sid      = "OperationLedger", Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"],
        Resource = [aws_dynamodb_table.control.arn]
      },
      {
        Sid      = "Evidence", Effect = "Allow", Action = ["s3:PutObject"],
        Resource = ["${aws_s3_bucket.control.arn}/evidence/*"]
      },
      {
        Sid      = "BuildLogs", Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"],
        Resource = ["${aws_cloudwatch_log_group.build.arn}:*"]
      },
      {
        Sid      = "OwnerVerification", Effect = "Allow", Action = ["states:DescribeExecution"],
        Resource = ["arn:aws:states:us-east-1:${local.account}:execution:${local.prefix}:*"]
      }
    ]
  })
}

resource "aws_iam_role" "workflow" {
  name = "${local.prefix}-workflow"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Principal = { Service = "states.amazonaws.com" }, Action = "sts:AssumeRole",
    Condition = { StringEquals = { "aws:SourceAccount" = local.account } }
  }] })
  tags = local.tags
}

resource "aws_iam_role_policy" "workflow" {
  role = aws_iam_role.workflow.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Effect   = "Allow", Action = ["codebuild:StartBuild", "codebuild:StopBuild", "codebuild:BatchGetBuilds"],
      Resource = [aws_codebuild_project.control.arn, "arn:aws:codebuild:us-east-1:${local.account}:build/${local.prefix}:*"]
    },
    {
      Effect   = "Allow", Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"],
      Resource = ["arn:aws:events:us-east-1:${local.account}:rule/StepFunctionsGetEventForCodeBuildStartBuildRule"]
    },
    { Effect = "Allow", Action = ["dynamodb:UpdateItem"], Resource = [aws_dynamodb_table.control.arn] }
  ] })
}
