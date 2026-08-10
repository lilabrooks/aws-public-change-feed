"""Cross-file Terraform contracts that native validation cannot prove."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TerraformContractTests(unittest.TestCase):
    @staticmethod
    def policy_statement(source, sid):
        marker = f'sid = "{sid}"'
        start = source.index(marker)
        end = source.find("\n  statement {", start)
        return source[start:] if end == -1 else source[start:end]

    def test_cloudwatch_alarms_can_publish_to_the_operations_topic(self):
        policy = (ROOT / "infra/central/sns.tf").read_text(encoding="utf-8")

        for required in (
            'sid       = "AllowCloudWatchAlarmPublish"',
            'type        = "Service"',
            'identifiers = ["cloudwatch.amazonaws.com"]',
            'variable = "aws:SourceAccount"',
            'variable = "aws:SourceArn"',
            ":alarm:apcf-${local.deployment_id}-*",
        ):
            with self.subTest(required=required):
                self.assertIn(required, policy)

    def test_backend_policy_covers_both_roots_and_native_lockfiles(self):
        bootstrap = (ROOT / "infra/bootstrap/main.tf").read_text(encoding="utf-8")
        central_backend = (ROOT / "infra/central/backend.tf").read_text(encoding="utf-8")

        self.assertIn('bootstrap_state_key = "apcf/terraform.tfstate"', bootstrap)
        self.assertIn('central_state_key   = "apcf/central/terraform.tfstate"', bootstrap)
        self.assertIn(
            "backend_state_keys  = [local.bootstrap_state_key, local.central_state_key]",
            bootstrap,
        )
        self.assertIn('key          = "apcf/central/terraform.tfstate"', central_backend)
        self.assertIn("use_lockfile = true", central_backend)
        state_actions = self.policy_statement(bootstrap, "StateObjectActions")
        lockfile_actions = self.policy_statement(bootstrap, "LockfileObjectActions")
        self.assertIn('"s3:GetObject"', state_actions)
        self.assertIn('"s3:PutObject"', state_actions)
        self.assertNotIn('"s3:DeleteObject"', state_actions)
        self.assertIn('"s3:DeleteObject"', lockfile_actions)
        self.assertIn('"${aws_s3_bucket.state.arn}/${key}.tflock"', lockfile_actions)
        self.assertIn("values   = local.backend_state_keys", bootstrap)
        self.assertNotIn('values   = ["apcf/*"]', bootstrap)

    def test_feed_dashboard_has_aggregate_and_per_feed_staleness(self):
        dashboard = (ROOT / "infra/central/dashboard.tf").read_text(encoding="utf-8")
        alarms = (ROOT / "infra/central/alarms.tf").read_text(encoding="utf-8")

        self.assertIn('"MaxFeedStalenessSeconds"', dashboard)
        self.assertIn(r"MetricName=\"FeedStalenessSeconds\"", dashboard)
        self.assertIn("{${local.metrics_namespace},FeedName}", dashboard)
        self.assertIn('metric_name         = "MaxFeedStalenessSeconds"', alarms)

    def test_delivery_due_key_is_numeric(self):
        table = (ROOT / "infra/central/dynamodb.tf").read_text(encoding="utf-8")
        self.assertIn('name = "next_action_at"\n    type = "N"', table)

    def test_fifo_worker_capacity_and_partial_failure_contract_are_wired_together(self):
        locals_source = (ROOT / "infra/central/locals.tf").read_text(encoding="utf-8")
        lambda_source = (ROOT / "infra/central/lambda.tf").read_text(encoding="utf-8")
        queue_source = (ROOT / "infra/central/sqs.tf").read_text(encoding="utf-8")

        self.assertIn("worker_timeout_seconds", locals_source)
        self.assertIn(
            "worker_visibility_seconds          = 6 * local.worker_timeout_seconds + local.worker_batch_window_seconds",
            locals_source,
        )
        self.assertIn("visibility_timeout_seconds  = local.worker_visibility_seconds", queue_source)
        self.assertIn('function_response_types = ["ReportBatchItemFailures"]', lambda_source)
        self.assertIn("batch_size                         = local.worker_batch_size", lambda_source)
        self.assertIn(
            "maximum_batching_window_in_seconds = local.worker_batch_window_seconds",
            lambda_source,
        )
        self.assertIn("maximum_concurrency = local.rate_control.worker_reserved_concurrency", lambda_source)

    def test_worker_uses_one_content_address_for_s3_version_and_runtime_gate(self):
        locals_source = (ROOT / "infra/central/locals.tf").read_text(encoding="utf-8")
        lambda_source = (ROOT / "infra/central/lambda.tf").read_text(encoding="utf-8")
        variables = (ROOT / "infra/central/variables.tf").read_text(encoding="utf-8")
        publisher_policy = (ROOT / "infra/central/iam.tf").read_text(encoding="utf-8")

        self.assertIn(
            'var.worker_artifact_sha256 == null ? null : "sha256:${var.worker_artifact_sha256}"', locals_source
        )
        self.assertIn("s3_key            = local.worker_artifact_key", lambda_source)
        self.assertIn("s3_object_version = var.worker_artifact_version_id", lambda_source)
        self.assertIn("APPLICATION_VERSION", lambda_source)
        self.assertIn(
            "(var.worker_artifact_sha256 == null) == (var.worker_artifact_version_id == null)",
            variables,
        )
        artifact_statement = publisher_policy[publisher_policy.index('sid     = "PublishApplicationArtifacts"') :]
        artifact_statement = artifact_statement[: artifact_statement.index("\n  }")]
        self.assertIn('actions = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]', artifact_statement)
        self.assertNotIn("s3:DeleteObject", artifact_statement)

    def test_watcher_uses_the_exact_worker_package_and_frozen_schedule(self):
        locals_source = (ROOT / "infra/central/locals.tf").read_text(encoding="utf-8")
        lambda_source = (ROOT / "infra/central/lambda.tf").read_text(encoding="utf-8")
        variables = (ROOT / "infra/central/variables.tf").read_text(encoding="utf-8")

        self.assertIn("watcher_timeout_seconds        = 300", locals_source)
        self.assertIn("watcher_reserved_concurrency   = 1", locals_source)
        self.assertIn("watcher_lease_seconds          = 360", locals_source)
        self.assertIn('watcher_schedule_expression    = "rate(15 minutes)"', locals_source)
        self.assertIn("watcher_maximum_retry_attempts = 2", locals_source)
        self.assertIn("watcher_maximum_event_age      = 900", locals_source)
        self.assertIn("s3_key            = local.watcher_artifact_key", lambda_source)
        self.assertIn("s3_object_version = var.watcher_artifact_version_id", lambda_source)
        self.assertIn("timeout                        = local.watcher_timeout_seconds", lambda_source)
        self.assertIn("reserved_concurrent_executions = local.watcher_reserved_concurrency", lambda_source)
        self.assertIn("maximum_event_age_in_seconds = local.watcher_maximum_event_age", lambda_source)
        self.assertIn("maximum_retry_attempts       = local.watcher_maximum_retry_attempts", lambda_source)
        self.assertIn(
            "(var.watcher_artifact_sha256 == null) == (var.watcher_artifact_version_id == null)",
            variables,
        )
        self.assertIn("var.watcher_artifact_sha256 == var.worker_artifact_sha256", variables)
        self.assertIn("var.watcher_artifact_version_id == var.worker_artifact_version_id", variables)
        watcher_rule = lambda_source[lambda_source.index('resource "aws_cloudwatch_event_rule" "watcher"') :]
        watcher_rule = watcher_rule[: watcher_rule.index("\nresource ", 1)]
        self.assertIn("count = local.watcher_runtime_enabled ? 1 : 0", watcher_rule)

    def test_watcher_receives_every_validated_fetch_policy_value(self):
        lambda_source = (ROOT / "infra/central/lambda.tf").read_text(encoding="utf-8")
        for name in (
            "APPROVED_FEED_HOSTS_JSON",
            "FEED_CONNECT_TIMEOUT_SECONDS",
            "FEED_RESPONSE_TIMEOUT_SECONDS",
            "MAX_CONCURRENT_FETCHES",
            "MAX_FEED_ITEM_CHARACTERS",
            "MAX_FEED_ITEMS",
            "MAX_FEED_REDIRECTS",
            "MAX_FEED_RESPONSE_BYTES",
            "RAW_SNAPSHOT_PREFIX",
        ):
            with self.subTest(name=name):
                self.assertIn(name, lambda_source)

    def test_watcher_failure_target_and_source_state_permissions_are_exact(self):
        queue = (ROOT / "infra/central/sqs.tf").read_text(encoding="utf-8")
        iam = (ROOT / "infra/central/iam.tf").read_text(encoding="utf-8")
        alarms = (ROOT / "infra/central/alarms.tf").read_text(encoding="utf-8")

        start = queue.index('sid     = "AllowExactWatcherSchedule"')
        watcher_statement = queue[start : queue.index("\n  statement {", start)]
        self.assertIn("values   = [aws_cloudwatch_event_rule.watcher[0].arn]", watcher_statement)
        self.assertNotIn("reconciler.arn", watcher_statement)
        feed_policy = iam[iam.index('data "aws_iam_policy_document" "feed_watcher"') :]
        feed_policy = feed_policy[: feed_policy.index('data "aws_iam_policy_document" "outbox_dispatcher"')]
        self.assertIn('"dynamodb:TransactWriteItems"', feed_policy)
        self.assertNotIn('"dynamodb:DeleteItem"', feed_policy)
        heartbeat = alarms[alarms.index('resource "aws_cloudwatch_metric_alarm" "feed_watcher_heartbeat"') :]
        heartbeat = heartbeat[: heartbeat.index("\nresource ", 1)]
        self.assertIn("count = local.watcher_runtime_enabled ? 1 : 0", heartbeat)

    def test_worker_metric_names_match_the_operator_alarms(self):
        runtime = (ROOT / "src/aws_public_change_feed/slack_worker_runtime.py").read_text(encoding="utf-8")
        alarms = (ROOT / "infra/central/alarms.tf").read_text(encoding="utf-8")
        dashboard = (ROOT / "infra/central/dashboard.tf").read_text(encoding="utf-8")

        for metric in (
            "DeliveryUnknown",
            "TerminalFailure",
            "ApplicationVersionMismatch",
            "ArtifactUnavailable",
            "ArtifactAvailabilityCheckFailed",
            "WorkerFault",
        ):
            with self.subTest(metric=metric):
                self.assertIn(f'"{metric}"', runtime)
                self.assertIn(f'metric_name         = "{metric}"', alarms)
                self.assertIn(f'"{metric}"', dashboard)

    def test_reconciler_has_its_own_exact_artifact_and_bounded_schedule(self):
        locals_source = (ROOT / "infra/central/locals.tf").read_text(encoding="utf-8")
        lambda_source = (ROOT / "infra/central/lambda.tf").read_text(encoding="utf-8")
        variables = (ROOT / "infra/central/variables.tf").read_text(encoding="utf-8")

        self.assertIn("reconciler_timeout_seconds        = 60", locals_source)
        self.assertIn("reconciler_reserved_concurrency   = 1", locals_source)
        self.assertIn("reconciler_repair_limit           = 100", locals_source)
        self.assertIn("reconciler_observation_limit      = 101", locals_source)
        self.assertIn('reconciler_schedule_expression    = "rate(5 minutes)"', locals_source)
        self.assertIn("reconciler_maximum_retry_attempts = 2", locals_source)
        self.assertIn("reconciler_maximum_event_age      = 300", locals_source)
        self.assertIn("s3_key            = local.reconciler_artifact_key", lambda_source)
        self.assertIn("s3_object_version = var.reconciler_artifact_version_id", lambda_source)
        self.assertIn("timeout                        = local.reconciler_timeout_seconds", lambda_source)
        self.assertIn("reserved_concurrent_executions = local.reconciler_reserved_concurrency", lambda_source)
        self.assertIn("maximum_event_age_in_seconds = local.reconciler_maximum_event_age", lambda_source)
        self.assertIn("maximum_retry_attempts       = local.reconciler_maximum_retry_attempts", lambda_source)
        self.assertIn(
            "(var.reconciler_artifact_sha256 == null) == (var.reconciler_artifact_version_id == null)",
            variables,
        )
        self.assertNotIn(
            "var.worker_artifact_version_id\n\n  timeout                        = local.reconciler", lambda_source
        )

    def test_reconciler_failure_queue_is_standard_encrypted_and_source_scoped(self):
        queue = (ROOT / "infra/central/sqs.tf").read_text(encoding="utf-8")
        lambda_source = (ROOT / "infra/central/lambda.tf").read_text(encoding="utf-8")

        runtime_queue = queue[queue.index('resource "aws_sqs_queue" "runtime_failures"') :]
        self.assertIn("sqs_managed_sse_enabled   = true", runtime_queue)
        self.assertNotIn("fifo_queue", runtime_queue.split("data ", 1)[0])
        self.assertIn('sid     = "AllowExactReconcilerSchedule"', queue)
        self.assertIn('identifiers = ["events.amazonaws.com"]', queue)
        self.assertIn('variable = "aws:SourceArn"', queue)
        self.assertIn("values   = [aws_cloudwatch_event_rule.reconciler.arn]", queue)
        self.assertIn("dead_letter_config", lambda_source)
        self.assertIn("arn = aws_sqs_queue.runtime_failures.arn", lambda_source)

    def test_reconciler_iam_has_no_scan_secret_or_external_network_permission(self):
        iam = (ROOT / "infra/central/iam.tf").read_text(encoding="utf-8")
        statement = iam[iam.index('data "aws_iam_policy_document" "recovery_reconciler"') :]
        statement = statement[: statement.index('\nresource "aws_iam_role_policy"')]

        self.assertIn('actions = ["dynamodb:Query"]', statement)
        self.assertIn('actions = ["dynamodb:GetItem"]', statement)
        self.assertIn('actions = ["dynamodb:UpdateItem"]', statement)
        self.assertIn('actions   = ["sqs:SendMessage"]', statement)
        for forbidden in ("dynamodb:Scan", "secretsmanager", "ssm:GetParameter", "execute-api", "lambda:Invoke"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, statement)

    def test_reconciler_metric_names_and_dimensions_match_consumers(self):
        runtime = (ROOT / "src/aws_public_change_feed/recovery_runtime.py").read_text(encoding="utf-8")
        alarms = (ROOT / "infra/central/alarms.tf").read_text(encoding="utf-8")
        dashboard = (ROOT / "infra/central/dashboard.tf").read_text(encoding="utf-8")

        for metric in (
            "DeliveryUnknown",
            "DispatchUnknownOutcome",
            "ExpiredLeaseUnknown",
            "RecoveryRepairLimitReached",
            "StateObservationSaturated",
            "ReconcilerFault",
        ):
            with self.subTest(metric=metric):
                self.assertIn(f'"{metric}"', runtime)
                self.assertIn(f'"{metric}"', dashboard)
        for alarmed in (
            "DeliveryUnknown",
            "DispatchUnknownOutcome",
            "RecoveryRepairLimitReached",
            "StateObservationSaturated",
            "ReconcilerFault",
        ):
            with self.subTest(alarmed=alarmed):
                self.assertIn(f'metric_name         = "{alarmed}"', alarms)
        self.assertIn('[["Function"]]', runtime)
        self.assertIn('[["State"]]', runtime)
        self.assertIn("[[]]", runtime)

    def test_every_custom_alarm_has_a_built_metric_producer(self):
        alarms = (ROOT / "infra/central/alarms.tf").read_text(encoding="utf-8")
        runtimes = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in (
                "src/aws_public_change_feed/watcher_runtime.py",
                "src/aws_public_change_feed/slack_worker_runtime.py",
                "src/aws_public_change_feed/recovery_runtime.py",
            )
        )
        planned_exceptions = {"OldestPendingDeliveryAgeSeconds"}
        custom_metric_lines = [
            line.split('"')[1] for line in alarms.splitlines() if line.strip().startswith("metric_name") and '"' in line
        ]
        custom_metrics = {
            name
            for name in custom_metric_lines
            if f'metric_name         = "{name}"' in alarms
            and name
            not in {
                "ApproximateAgeOfOldestMessage",
                "ApproximateNumberOfMessagesVisible",
                "Errors",
                "WriteThrottleEvents",
            }
        }
        for metric in sorted(custom_metrics - planned_exceptions):
            with self.subTest(metric=metric):
                self.assertIn(f'"{metric}"', runtimes)


if __name__ == "__main__":
    unittest.main()
