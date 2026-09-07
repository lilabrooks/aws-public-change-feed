locals {
  build_parameters = {
    ProjectName                = aws_codebuild_project.control.name
    "SourceLocationOverride.$" = "$.source"
    "SourceVersion.$"          = "$.version"
    EnvironmentVariablesOverride = [
      { Name = "LIVE_OPERATION", "Value.$" = "$.operation", Type = "PLAINTEXT" },
      { Name = "LIVE_EXECUTION", "Value.$" = "$$.Execution.Id", Type = "PLAINTEXT" },
    ]
  }
  park_task = {
    Type = "Task", Resource = "arn:aws:states:::codebuild:startBuild.sync",
    Parameters = merge(local.build_parameters, {
      EnvironmentVariablesOverride = concat(local.build_parameters.EnvironmentVariablesOverride, [
        { Name = "LIVE_ACTION", Value = "park", Type = "PLAINTEXT" }
      ])
    }),
    ResultPath = null,
    Retry = [{
      ErrorEquals = ["States.ALL"], IntervalSeconds = local.timing.park_retry_interval_seconds,
      BackoffRate = local.timing.park_retry_backoff_rate, MaxAttempts = local.timing.park_retry_attempts
    }],
    Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "NotifyCleanupFailure" }],
    End   = true
  }
}

resource "aws_sfn_state_machine" "control" {
  name     = local.prefix
  role_arn = aws_iam_role.workflow.arn
  type     = "STANDARD"
  definition = jsonencode({
    Comment = "One durable owner. Early park sets a stop flag; it never cancels the cleanup owner.",
    StartAt = "Select",
    States = {
      Select = {
        Type    = "Choice", Choices = [{ Variable = "$.action", StringEquals = "park", Next = "Park" }],
        Default = "Activate"
      },
      Activate = {
        Type = "Task", Resource = "arn:aws:states:::codebuild:startBuild.sync",
        Parameters = merge(local.build_parameters, {
          EnvironmentVariablesOverride = concat(local.build_parameters.EnvironmentVariablesOverride, [
            { Name = "LIVE_ACTION", Value = "unpark", Type = "PLAINTEXT" }
          ])
        }),
        ResultPath = null,
        Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.activation_error", Next = "ParkAfterFailure" }],
        Next       = "AwaitPark"
      },
      AwaitPark = {
        Type           = "Task", QueryLanguage = "JSONata",
        Resource       = "arn:aws:states:::aws-sdk:dynamodb:updateItem.waitForTaskToken",
        TimeoutSeconds = "{% $max([1, $floor(($toMillis($states.input.cleanup_at) - $millis()) / 1000)]) %}",
        Arguments = {
          TableName                = aws_dynamodb_table.control.name, Key = { id = { S = "dev" } },
          UpdateExpression         = "SET callback_token = :token",
          ConditionExpression      = "generation = :generation AND #stop = :no",
          ExpressionAttributeNames = { "#stop" = "stop" },
          ExpressionAttributeValues = {
            ":token"      = { S = "{% $states.context.Task.Token %}" },
            ":generation" = { S = "{% $states.input.operation %}" },
            ":no"         = { Bool = false }
          }
        },
        Output = "{% $states.input %}",
        Catch = [
          { ErrorEquals = ["States.Timeout", "DynamoDb.ConditionalCheckFailedException"], Next = "Park", Output = "{% $states.input %}" },
          { ErrorEquals = ["States.ALL"], Next = "ParkAfterFailure", Output = "{% $states.input %}" }
        ],
        Next = "Park"
      },
      Park             = local.park_task,
      ParkAfterFailure = merge({ for key, value in local.park_task : key => value if key != "End" }, { Next = "Failed" }),
      Failed           = { Type = "Fail", Error = "LiveWindowFailed", Cause = "Activation or observation failed; parking completed separately." },
      NotifyCleanupFailure = {
        Type = "Task", Resource = "arn:aws:states:::sns:publish",
        Parameters = {
          TopicArn    = local.operations_topic_arn,
          Subject     = "APCF cleanup exhausted: check parking",
          "Message.$" = "States.Format('Cleanup retries exhausted. Execution: {}. Run live-status and recover with live-park. https://${local.region}.console.aws.amazon.com/states/home?region=${local.region}', $$.Execution.Id)"
        },
        TimeoutSeconds = local.timing.cleanup_notification_seconds,
        ResultPath     = null,
        Catch          = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "CleanupFailed" }],
        Next           = "CleanupFailed"
      },
      CleanupFailed = { Type = "Fail", Error = "LiveCleanupFailed", Cause = "Parking not confirmed. Recover with live-park." }
    }
  })
  tags = local.tags
}
