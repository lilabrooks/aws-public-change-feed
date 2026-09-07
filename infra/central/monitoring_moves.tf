moved {
  from = aws_cloudwatch_metric_alarm.delivery_queue_age
  to   = aws_cloudwatch_metric_alarm.delivery_queue_age[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.outbox_backlog_age
  to   = aws_cloudwatch_metric_alarm.outbox_backlog_age[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.delivery_dlq_depth
  to   = aws_cloudwatch_metric_alarm.delivery_dlq_depth[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.runtime_failure_queue_depth
  to   = aws_cloudwatch_metric_alarm.runtime_failure_queue_depth[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.worker_errors
  to   = aws_cloudwatch_metric_alarm.worker_errors[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.reconciler_errors
  to   = aws_cloudwatch_metric_alarm.reconciler_errors[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.source_state_write_throttles
  to   = aws_cloudwatch_metric_alarm.source_state_write_throttles[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.delivery_write_throttles
  to   = aws_cloudwatch_metric_alarm.delivery_write_throttles[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.delivery_unknown
  to   = aws_cloudwatch_metric_alarm.delivery_unknown[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.application_version_mismatch
  to   = aws_cloudwatch_metric_alarm.application_version_mismatch[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.artifact_unavailable
  to   = aws_cloudwatch_metric_alarm.artifact_unavailable[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.artifact_availability_check_failed
  to   = aws_cloudwatch_metric_alarm.artifact_availability_check_failed[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.worker_fault
  to   = aws_cloudwatch_metric_alarm.worker_fault[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.dispatch_unknown_outcome
  to   = aws_cloudwatch_metric_alarm.dispatch_unknown_outcome[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.recovery_observation_saturated
  to   = aws_cloudwatch_metric_alarm.recovery_observation_saturated[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.recovery_repair_limit
  to   = aws_cloudwatch_metric_alarm.recovery_repair_limit[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.reconciler_fault
  to   = aws_cloudwatch_metric_alarm.reconciler_fault[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.release_verification_failures
  to   = aws_cloudwatch_metric_alarm.release_verification_failures[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.raw_snapshot_failures
  to   = aws_cloudwatch_metric_alarm.raw_snapshot_failures[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.terminal_failures
  to   = aws_cloudwatch_metric_alarm.terminal_failures[0]
}

moved {
  from = aws_cloudwatch_metric_alarm.feed_staleness
  to   = aws_cloudwatch_metric_alarm.feed_staleness[0]
}

moved {
  from = aws_cloudwatch_dashboard.operations
  to   = aws_cloudwatch_dashboard.operations[0]
}
