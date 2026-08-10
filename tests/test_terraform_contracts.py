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


if __name__ == "__main__":
    unittest.main()
