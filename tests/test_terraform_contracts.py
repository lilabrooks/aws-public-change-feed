"""Cross-file Terraform contracts that native validation cannot prove."""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TerraformContractTests(unittest.TestCase):
    @staticmethod
    def resource_block(source, resource_type, resource_name):
        marker = f'resource "{resource_type}" "{resource_name}"'
        start = source.index(marker)
        end = source.find("\nresource ", start + 1)
        return source[start:] if end == -1 else source[start:end]

    @staticmethod
    def variable_block(source, variable_name):
        marker = f'variable "{variable_name}"'
        start = source.index(marker)
        end = source.find("\nvariable ", start + 1)
        return source[start:] if end == -1 else source[start:end]

    def assert_dimensionless_alarm(self, block):
        self.assertIsNone(
            re.search(r"(?m)^[ \t]*dimensions[ \t]*=", block),
            msg="dimensionless alarms cannot declare a dimensions attribute",
        )

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

    def test_terraform_check_keeps_committed_provider_locks_readonly(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            terraform = temporary_root / "terraform"
            argument_log = temporary_root / "arguments.log"
            terraform.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TERRAFORM_ARGUMENT_LOG"\n',
                encoding="utf-8",
            )
            terraform.chmod(0o700)
            environment = os.environ.copy()
            environment["TERRAFORM_ARGUMENT_LOG"] = str(argument_log)

            checked = subprocess.run(
                ("make", "terraform-check", f"TERRAFORM={terraform}"),
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(checked.returncode, 0, msg=f"{checked.stdout}\n{checked.stderr}")
            init_calls = [line for line in argument_log.read_text(encoding="utf-8").splitlines() if " init " in line]
            self.assertEqual(
                init_calls,
                [
                    "-chdir=infra/bootstrap init -backend=false -input=false -lockfile=readonly",
                    "-chdir=infra/central init -backend=false -input=false -lockfile=readonly",
                ],
            )

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

    def test_dispatcher_uses_the_exact_worker_package_and_frozen_schedule(self):
        locals_source = (ROOT / "infra/central/locals.tf").read_text(encoding="utf-8")
        lambda_source = (ROOT / "infra/central/lambda.tf").read_text(encoding="utf-8")
        variables = (ROOT / "infra/central/variables.tf").read_text(encoding="utf-8")
        queue = (ROOT / "infra/central/sqs.tf").read_text(encoding="utf-8")

        for frozen in (
            "dispatcher_timeout_seconds        = 60",
            "dispatcher_reserved_concurrency   = 1",
            'dispatcher_schedule_expression    = "rate(1 minute)"',
            "dispatcher_maximum_retry_attempts = 2",
            "dispatcher_maximum_event_age      = 300",
        ):
            with self.subTest(frozen=frozen):
                self.assertIn(frozen, locals_source)

        self.assertIn(
            "dispatcher_runtime_enabled        = var.dispatcher_artifact_sha256 != null && var.dispatcher_artifact_version_id != null",
            locals_source,
        )
        self.assertIn(
            "(var.dispatcher_artifact_sha256 == null) == (var.dispatcher_artifact_version_id == null)",
            variables,
        )
        self.assertIn("var.dispatcher_artifact_sha256 == var.worker_artifact_sha256", variables)
        self.assertIn("var.dispatcher_artifact_version_id == var.worker_artifact_version_id", variables)

        runtime_resources = (
            ("aws_lambda_function", "dispatcher"),
            ("aws_cloudwatch_event_rule", "dispatcher"),
            ("aws_cloudwatch_event_target", "dispatcher"),
            ("aws_lambda_permission", "dispatcher_schedule"),
        )
        for resource_type, name in runtime_resources:
            with self.subTest(resource=name):
                block = self.resource_block(lambda_source, resource_type, name)
                self.assertIn("count = local.dispatcher_runtime_enabled ? 1 : 0", block)

        dispatcher = self.resource_block(lambda_source, "aws_lambda_function", "dispatcher")
        for required in (
            'handler       = "aws_public_change_feed.dispatcher_runtime.lambda_handler"',
            "s3_key            = local.dispatcher_artifact_key",
            "s3_object_version = var.dispatcher_artifact_version_id",
            "timeout                        = local.dispatcher_timeout_seconds",
            "reserved_concurrent_executions = local.dispatcher_reserved_concurrency",
            "DELIVERY_INDEX_NAME",
            "DELIVERY_QUEUE_URL",
            "DELIVERY_TABLE_NAME",
            "MAX_DELIVERY_REQUEST_BYTES",
            "METRICS_NAMESPACE",
        ):
            with self.subTest(required=required):
                self.assertIn(required, dispatcher)

        target = self.resource_block(lambda_source, "aws_cloudwatch_event_target", "dispatcher")
        self.assertIn("maximum_event_age_in_seconds = local.dispatcher_maximum_event_age", target)
        self.assertIn("maximum_retry_attempts       = local.dispatcher_maximum_retry_attempts", target)
        self.assertIn("arn = aws_sqs_queue.runtime_failures.arn", target)
        permission = self.resource_block(lambda_source, "aws_lambda_permission", "dispatcher_schedule")
        self.assertIn("source_arn    = aws_cloudwatch_event_rule.dispatcher[0].arn", permission)

        statement = queue[queue.index('sid     = "AllowExactDispatcherSchedule"') :]
        statement = statement[: statement.index("\n  statement {")]
        self.assertIn("values   = [aws_cloudwatch_event_rule.dispatcher[0].arn]", statement)
        self.assertIn('variable = "aws:SourceAccount"', statement)

    def test_dispatcher_artifact_validations_execute_in_provider_free_plans(self):
        variables = (ROOT / "infra/central/variables.tf").read_text(encoding="utf-8")
        artifact_variables = "\n\n".join(
            self.variable_block(variables, name)
            for name in (
                "worker_artifact_sha256",
                "worker_artifact_version_id",
                "dispatcher_artifact_sha256",
                "dispatcher_artifact_version_id",
            )
        )
        digest_a = "a" * 64
        digest_b = "b" * 64
        cases = (
            ("null pairs", (), 0, None),
            (
                "equal pairs",
                (
                    f"worker_artifact_sha256={digest_a}",
                    "worker_artifact_version_id=version-1",
                    f"dispatcher_artifact_sha256={digest_a}",
                    "dispatcher_artifact_version_id=version-1",
                ),
                0,
                None,
            ),
            (
                "malformed dispatcher digest",
                (
                    f"worker_artifact_sha256={digest_a}",
                    "worker_artifact_version_id=version-1",
                    "dispatcher_artifact_sha256=not-a-sha256",
                    "dispatcher_artifact_version_id=version-1",
                ),
                1,
                "dispatcher_artifact_sha256 must be null or exactly 64 lowercase hexadecimal characters.",
            ),
            (
                "incomplete dispatcher pair",
                (
                    f"worker_artifact_sha256={digest_a}",
                    "worker_artifact_version_id=version-1",
                    f"dispatcher_artifact_sha256={digest_a}",
                ),
                1,
                "dispatcher_artifact_sha256 and dispatcher_artifact_version_id must both be set or both be null.",
            ),
            (
                "unequal digest",
                (
                    f"worker_artifact_sha256={digest_a}",
                    "worker_artifact_version_id=version-1",
                    f"dispatcher_artifact_sha256={digest_b}",
                    "dispatcher_artifact_version_id=version-1",
                ),
                1,
                "enabling the dispatcher requires the exact same artifact digest as the enabled Slack worker.",
            ),
            (
                "unequal version ID",
                (
                    f"worker_artifact_sha256={digest_a}",
                    "worker_artifact_version_id=version-1",
                    f"dispatcher_artifact_sha256={digest_a}",
                    "dispatcher_artifact_version_id=version-2",
                ),
                1,
                "enabling the dispatcher requires the exact same S3 VersionId as the enabled Slack worker.",
            ),
            (
                "dispatcher without worker",
                (
                    f"dispatcher_artifact_sha256={digest_a}",
                    "dispatcher_artifact_version_id=version-1",
                ),
                1,
                "enabling the dispatcher requires the exact same S3 VersionId as the enabled Slack worker.",
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tf").write_text(f"{artifact_variables}\n", encoding="utf-8")
            environment = {**os.environ, "CHECKPOINT_DISABLE": "1", "TF_IN_AUTOMATION": "1"}
            initialized = subprocess.run(
                ("terraform", "init", "-backend=false", "-input=false", "-no-color"),
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(
                initialized.returncode,
                0,
                msg=f"provider-free terraform init failed:\n{initialized.stdout}\n{initialized.stderr}",
            )

            for name, values, expected_exit, expected_message in cases:
                with self.subTest(case=name):
                    command = [
                        "terraform",
                        "plan",
                        "-input=false",
                        "-lock=false",
                        "-refresh=false",
                        "-no-color",
                    ]
                    command.extend(f"-var={value}" for value in values)
                    result = subprocess.run(
                        command,
                        cwd=root,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )
                    output = f"{result.stdout}\n{result.stderr}"
                    self.assertEqual(result.returncode, expected_exit, msg=output)
                    if expected_message is not None:
                        normalized_output = re.sub(r"\s+", " ", output)
                        self.assertIn(expected_message, normalized_output)

    def test_dispatcher_errors_and_heartbeat_share_runtime_enablement(self):
        alarms = (ROOT / "infra/central/alarms.tf").read_text(encoding="utf-8")
        lambda_source = (ROOT / "infra/central/lambda.tf").read_text(encoding="utf-8")
        condition = "count = local.dispatcher_runtime_enabled ? 1 : 0"

        for resource_type, source, name in (
            ("aws_lambda_function", lambda_source, "dispatcher"),
            ("aws_cloudwatch_metric_alarm", alarms, "dispatcher_errors"),
            ("aws_cloudwatch_metric_alarm", alarms, "dispatcher_heartbeat"),
        ):
            with self.subTest(resource=name):
                self.assertIn(condition, self.resource_block(source, resource_type, name))

        heartbeat = self.resource_block(alarms, "aws_cloudwatch_metric_alarm", "dispatcher_heartbeat")
        self.assertIn('metric_name         = "Heartbeat"', heartbeat)
        self.assertIn("Function = local.function_names.dispatcher", heartbeat)

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

    def test_reconciler_heartbeat_uses_the_exact_runtime_enablement_condition(self):
        alarms = (ROOT / "infra/central/alarms.tf").read_text(encoding="utf-8")
        lambda_source = (ROOT / "infra/central/lambda.tf").read_text(encoding="utf-8")
        heartbeat = self.resource_block(alarms, "aws_cloudwatch_metric_alarm", "reconciler_heartbeat")
        reconciler = self.resource_block(lambda_source, "aws_lambda_function", "reconciler")

        condition = "count = local.reconciler_runtime_enabled ? 1 : 0"
        self.assertIn(condition, reconciler)
        self.assertIn(condition, heartbeat)

    def test_watcher_failure_alarms_match_the_accepted_paging_policy(self):
        alarms = (ROOT / "infra/central/alarms.tf").read_text(encoding="utf-8")
        errors = self.resource_block(alarms, "aws_cloudwatch_metric_alarm", "feed_watcher_errors")
        incomplete = self.resource_block(alarms, "aws_cloudwatch_metric_alarm", "watcher_incomplete_runs")
        fault = self.resource_block(alarms, "aws_cloudwatch_metric_alarm", "watcher_fault")

        for block in (errors, incomplete):
            with self.subTest(alarm="sustained"):
                self.assertIn("evaluation_periods  = 2", block)
                self.assertIn("datapoints_to_alarm = 2", block)
                self.assertIn("period              = 900", block)
                self.assertIn('statistic           = "Sum"', block)
                self.assertIn("threshold           = 0", block)
                self.assertIn('treat_missing_data  = "notBreaching"', block)
                self.assertIn("alarm_actions             = [aws_sns_topic.operations.arn]", block)
        self.assertIn('namespace           = "AWS/Lambda"', errors)
        self.assertIn("FunctionName = local.function_names.watcher", errors)
        self.assertIn('metric_name         = "IncompleteRuns"', incomplete)
        self.assertIn("namespace           = local.metrics_namespace", incomplete)
        self.assertIn("Function = local.function_names.watcher", incomplete)
        self.assertIn("count = local.watcher_runtime_enabled ? 1 : 0", incomplete)

        self.assertIn("evaluation_periods  = 1", fault)
        self.assertIn("datapoints_to_alarm = 1", fault)
        self.assertIn("period              = 300", fault)
        self.assertIn('metric_name         = "WatcherFaults"', fault)
        self.assertIn("namespace           = local.metrics_namespace", fault)
        self.assertIn("Function = local.function_names.watcher", fault)
        self.assertIn("count = local.watcher_runtime_enabled ? 1 : 0", fault)
        self.assertIn('treat_missing_data  = "notBreaching"', fault)
        self.assertIn("alarm_actions             = [aws_sns_topic.operations.arn]", fault)

    def test_changed_watcher_custom_alarms_are_owned_by_the_watcher_runtime(self):
        watcher = (ROOT / "src/aws_public_change_feed/watcher_runtime.py").read_text(encoding="utf-8")
        other_runtimes = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in (
                "src/aws_public_change_feed/slack_worker_runtime.py",
                "src/aws_public_change_feed/recovery_runtime.py",
            )
        )
        for metric in ("IncompleteRuns", "WatcherFaults"):
            with self.subTest(metric=metric):
                self.assertIn(f'"{metric}"', watcher)
                self.assertNotIn(f'"{metric}"', other_runtimes)

    def test_watcher_fault_runbook_preserves_the_diagnostic_chain(self):
        runbook = (ROOT / "docs/runbooks/operations.md").read_text(encoding="utf-8")
        start = runbook.index("## Feed stale or fetch failing")
        section = runbook[start : runbook.index("\n## Feed appears quiet", start)]

        for evidence in (
            "WatcherFaults",
            "IncompleteRuns",
            "AWS/Lambda `Errors`",
            "MaxFeedStalenessSeconds",
            "absence is unknown",
            "last success",
            "lease owner",
        ):
            with self.subTest(evidence=evidence):
                self.assertIn(evidence, section)

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

    def assert_custom_alarm_contracts(self, alarms):
        runtimes = {
            "watcher": (ROOT / "src/aws_public_change_feed/watcher_runtime.py").read_text(encoding="utf-8"),
            "dispatcher": (ROOT / "src/aws_public_change_feed/dispatcher_runtime.py").read_text(encoding="utf-8"),
            "worker": (ROOT / "src/aws_public_change_feed/slack_worker_runtime.py").read_text(encoding="utf-8"),
            "reconciler": (ROOT / "src/aws_public_change_feed/recovery_runtime.py").read_text(encoding="utf-8"),
        }
        expected = {
            "outbox_backlog_age": ("OldestPendingDeliveryAgeSeconds", (), {"reconciler"}),
            "watcher_incomplete_runs": ("IncompleteRuns", ("Function",), {"watcher"}),
            "watcher_fault": ("WatcherFaults", ("Function",), {"watcher"}),
            "dispatcher_heartbeat": ("Heartbeat", ("Function",), {"dispatcher"}),
            "feed_watcher_heartbeat": ("Heartbeat", ("Function",), {"watcher"}),
            "reconciler_heartbeat": ("Heartbeat", ("Function",), {"reconciler"}),
            "delivery_unknown": ("DeliveryUnknown", (), {"worker", "reconciler"}),
            "application_version_mismatch": ("ApplicationVersionMismatch", (), {"worker"}),
            "artifact_unavailable": ("ArtifactUnavailable", (), {"worker"}),
            "artifact_availability_check_failed": ("ArtifactAvailabilityCheckFailed", (), {"worker"}),
            "worker_fault": ("WorkerFault", (), {"worker"}),
            "dispatch_unknown_outcome": ("DispatchUnknownOutcome", (), {"dispatcher", "reconciler"}),
            "recovery_observation_saturated": ("StateObservationSaturated", (), {"reconciler"}),
            "recovery_repair_limit": ("RecoveryRepairLimitReached", (), {"reconciler"}),
            "reconciler_fault": ("ReconcilerFault", (), {"reconciler"}),
            "release_verification_failures": ("ReleaseVerificationFailures", (), {"watcher"}),
            "raw_snapshot_failures": ("RawSnapshotFailures", (), {"watcher"}),
            "terminal_failures": ("TerminalFailure", (), {"worker"}),
            "feed_staleness": ("MaxFeedStalenessSeconds", (), {"watcher"}),
        }
        alarm_names = {
            line.split('"')[3]
            for line in alarms.splitlines()
            if line.startswith('resource "aws_cloudwatch_metric_alarm"')
        }
        custom_names = {
            name
            for name in alarm_names
            if re.search(
                r"(?m)^[ \t]*namespace[ \t]*=[ \t]*local\.metrics_namespace(?=[ \t]*(?:$|#|//|/\*))",
                self.resource_block(alarms, "aws_cloudwatch_metric_alarm", name),
            )
        }
        self.assertEqual(custom_names, set(expected), msg="every custom alarm needs an explicit ownership contract")

        function_owner = {
            "dispatcher_heartbeat": "dispatcher",
            "feed_watcher_heartbeat": "watcher",
            "reconciler_heartbeat": "reconciler",
            "watcher_incomplete_runs": "watcher",
            "watcher_fault": "watcher",
        }
        for name, (metric, dimensions, eligible) in expected.items():
            with self.subTest(alarm=name):
                block = self.resource_block(alarms, "aws_cloudwatch_metric_alarm", name)
                self.assertIn(f'metric_name         = "{metric}"', block)
                if dimensions:
                    owner = function_owner[name]
                    self.assertIn(f"Function = local.function_names.{owner}", block)
                    self.assertEqual(eligible, {owner})
                    self.assertIn(f'"{metric}"', runtimes[owner])
                    self.assertIn('[["Function"]]', runtimes[owner])
                else:
                    self.assert_dimensionless_alarm(block)
                    actual_producers = {runtime for runtime, source in runtimes.items() if f'"{metric}"' in source}
                    self.assertEqual(actual_producers, eligible)

    def test_every_custom_alarm_has_an_exact_dimension_and_eligible_producer_set(self):
        alarms = (ROOT / "infra/central/alarms.tf").read_text(encoding="utf-8")
        self.assert_custom_alarm_contracts(alarms)

    def test_unregistered_custom_alarm_variants_cannot_escape_ownership_registration(self):
        alarms = (ROOT / "infra/central/alarms.tf").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            for name, comment in (
                ("regrouped", ""),
                ("hash_comment", " # shared application namespace"),
                ("slash_comment", " // shared application namespace"),
                ("block_comment", " /* shared application namespace */"),
            ):
                with self.subTest(variant=name):
                    unregistered_alarm = f'''
resource "aws_cloudwatch_metric_alarm" "{name}_unregistered" {{
  alarm_name          = "{name.replace("_", "-")}-unregistered"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "TotallyUnregisteredMetric"

  namespace = local.metrics_namespace{comment}

  period             = 60
  statistic          = "Sum"
  threshold          = 0
  treat_missing_data = "notBreaching"
}}
'''
                    fixture = Path(directory) / f"{name}.tf"
                    fixture.write_text(unregistered_alarm, encoding="utf-8")
                    formatted = subprocess.run(
                        ("terraform", "fmt", "-check", str(fixture)),
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )
                    self.assertEqual(formatted.returncode, 0, msg=f"{formatted.stdout}\n{formatted.stderr}")
                    with self.assertRaisesRegex(
                        AssertionError,
                        "every custom alarm needs an explicit ownership contract",
                    ):
                        self.assert_custom_alarm_contracts(f"{alarms}\n{unregistered_alarm}")

    def test_a_single_line_dimension_cannot_escape_the_dimensionless_contract(self):
        alarms = (ROOT / "infra/central/alarms.tf").read_text(encoding="utf-8")
        terminal = self.resource_block(alarms, "aws_cloudwatch_metric_alarm", "terminal_failures")
        mutated_terminal = terminal.replace(
            "  period              = 300",
            "  dimensions          = { Function = local.function_names.worker }\n  period              = 300",
            1,
        )
        self.assertNotEqual(mutated_terminal, terminal)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "single-line-dimension.tf"
            fixture.write_text(mutated_terminal, encoding="utf-8")
            formatted = subprocess.run(
                ("terraform", "fmt", "-check", str(fixture)),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(formatted.returncode, 0, msg=f"{formatted.stdout}\n{formatted.stderr}")

        with self.assertRaisesRegex(AssertionError, "dimensionless alarms cannot declare a dimensions attribute"):
            self.assert_dimensionless_alarm(mutated_terminal)


if __name__ == "__main__":
    unittest.main()
