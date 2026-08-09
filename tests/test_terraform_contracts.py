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


if __name__ == "__main__":
    unittest.main()
