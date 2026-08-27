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

    @staticmethod
    def terraform_root_sources():
        return {path.name: path.read_text(encoding="utf-8") for path in sorted((ROOT / "infra/central").glob("*.tf"))}

    def assert_delivery_trigger_alarm_inventory_matches_runbook(self, terraform_sources, step_6):
        alarm_declarations = [
            (file_name, match.group(1))
            for file_name, source in sorted(terraform_sources.items())
            for match in re.finditer(
                r'(?m)^resource "aws_cloudwatch_metric_alarm" "([^"\n]+)" \{(?:[ \t]*(?://|#).*)?$',
                source,
            )
        ]
        self.assertEqual(
            {file_name for file_name, _ in alarm_declarations},
            {"alarms.tf"},
            msg="root CloudWatch alarm declarations must remain in alarms.tf",
        )
        alarm_names = [name for _, name in alarm_declarations]
        self.assertEqual(
            len(alarm_names),
            len(set(alarm_names)),
            msg="root CloudWatch alarm resource names must be unique",
        )

        alarms = terraform_sources["alarms.tf"]
        delivery_count = re.compile(r"local\.(?:watcher|dispatcher|worker)_trigger_enabled \? 1 : 0")
        delivery_trigger_alarms = set()
        for name in alarm_names:
            block = self.resource_block(alarms, "aws_cloudwatch_metric_alarm", name)
            cardinality_selectors = re.findall(r"(?m)^  (count|for_each)[ \t]*=[ \t]*(.*)$", block)
            if not cardinality_selectors:
                continue
            if cardinality_selectors == [("count", "local.reconciler_trigger_enabled ? 1 : 0")]:
                continue
            if (
                len(cardinality_selectors) == 1
                and cardinality_selectors[0][0] == "count"
                and delivery_count.fullmatch(cardinality_selectors[0][1])
            ):
                delivery_trigger_alarms.add(name)
                continue
            self.fail(f"root CloudWatch alarm {name} has an unclassified cardinality selector: {cardinality_selectors}")

        documented_addresses = re.findall(r"`aws_cloudwatch_metric_alarm\.([a-z0-9_]+)`", step_6)
        documented_pairs = re.findall(
            r"`aws_cloudwatch_metric_alarm\.([a-z0-9_]+)` \(`(apcf-<deployment>-[a-z0-9-]+)`\)",
            step_6,
        )
        self.assertEqual(
            len(documented_pairs),
            len(documented_addresses),
            msg="runbook step 6 must pair every alarm address occurrence with a parseable CloudWatch name",
        )
        pair_addresses = [address for address, _ in documented_pairs]
        self.assertEqual(
            len(pair_addresses),
            len(set(pair_addresses)),
            msg="runbook step 6 must name every alarm address exactly once",
        )
        documented_alarms = dict(documented_pairs)
        source_alarm_names = {}
        for name in delivery_trigger_alarms:
            block = self.resource_block(alarms, "aws_cloudwatch_metric_alarm", name)
            match = re.search(r'(?m)^[ \t]*alarm_name[ \t]*=[ \t]*"([^"]+)"$', block)
            if match is None:
                self.fail(f"delivery-gated alarm {name} must declare alarm_name")
            source_alarm_names[name] = match.group(1).replace("${local.deployment_id}", "<deployment>")

        self.assertEqual(
            set(documented_alarms),
            set(source_alarm_names),
            msg="runbook step 6 must name exactly every delivery-gated alarm resource",
        )
        self.assertEqual(
            documented_alarms,
            source_alarm_names,
            msg="runbook step 6 must pair every delivery-gated alarm with its CloudWatch name pattern",
        )

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

    def test_operations_subscriptions_bind_reviewed_aliases_to_private_endpoints(self):
        variables = (ROOT / "infra/central/variables.tf").read_text(encoding="utf-8")
        locals_source = (ROOT / "infra/central/locals.tf").read_text(encoding="utf-8")
        sns = (ROOT / "infra/central/sns.tf").read_text(encoding="utf-8")
        endpoints = self.variable_block(variables, "operational_sns_subscription_endpoints")
        subscription = self.resource_block(sns, "aws_sns_topic_subscription", "operations")

        self.assertIn("type        = map(string)", endpoints)
        self.assertIn("default     = {}", endpoints)
        self.assertIn("sensitive   = true", endpoints)
        self.assertIn("length(trimspace(endpoint)) > 0", endpoints)
        self.assertIn("for subscription in local.deployment.operational_sns_subscriptions", locals_source)
        self.assertIn("toset(keys(local.operational_sns_subscriptions))", locals_source)
        self.assertIn("toset(keys(var.operational_sns_subscription_endpoints))", locals_source)
        self.assertIn("for_each = local.operational_sns_subscriptions", subscription)
        self.assertIn("topic_arn = aws_sns_topic.operations.arn", subscription)
        self.assertIn("protocol  = each.value.protocol", subscription)
        self.assertIn('endpoint  = lookup(var.operational_sns_subscription_endpoints, each.key, "")', subscription)
        self.assertIn(
            "local.operational_sns_subscription_aliases == local.operational_sns_endpoint_aliases",
            subscription,
        )
        self.assertIn("keys must exactly match", subscription)

    def test_backend_policy_covers_all_three_roots_and_native_lockfiles(self):
        bootstrap = (ROOT / "infra/bootstrap/main.tf").read_text(encoding="utf-8")
        central_backend = (ROOT / "infra/central/backend.tf").read_text(encoding="utf-8")
        preflight_backend = (ROOT / "infra/preflight/backend.tf").read_text(encoding="utf-8")

        self.assertIn('bootstrap_state_key = "apcf/terraform.tfstate"', bootstrap)
        self.assertIn('central_state_key   = "apcf/central/terraform.tfstate"', bootstrap)
        self.assertIn('preflight_state_key = "apcf/preflight/terraform.tfstate"', bootstrap)
        self.assertIn(
            "backend_state_keys  = [local.bootstrap_state_key, local.central_state_key, local.preflight_state_key]",
            bootstrap,
        )
        self.assertIn('key          = "apcf/central/terraform.tfstate"', central_backend)
        self.assertIn('key          = "apcf/preflight/terraform.tfstate"', preflight_backend)
        self.assertIn("use_lockfile = true", central_backend)
        self.assertIn("use_lockfile = true", preflight_backend)
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
                    "-chdir=infra/preflight init -backend=false -input=false -lockfile=readonly",
                ],
            )

    def test_preflight_root_has_exact_state_resource_and_trigger_boundaries(self):
        main = (ROOT / "infra/preflight/main.tf").read_text(encoding="utf-8")
        variables = (ROOT / "infra/preflight/variables.tf").read_text(encoding="utf-8")
        deployment = (ROOT / "infra/preflight/deployment.yaml").read_text(encoding="utf-8")
        central_lambda = (ROOT / "infra/central/lambda.tf").read_text(encoding="utf-8")
        central_s3 = (ROOT / "infra/central/s3.tf").read_text(encoding="utf-8")

        self.assertIn('state_key       = "apcf/preflight/terraform.tfstate"', main)
        self.assertIn('source = "../central"', main)
        self.assertIn("preflight_mode               = true", main)
        self.assertIn("runtime_artifact_bucket_name = var.runtime_artifact_bucket_name", main)
        self.assertIn("watcher_trigger_enabled_override    = false", main)
        self.assertIn("dispatcher_trigger_enabled_override = var.exercise_load_triggers_enabled", main)
        self.assertIn("worker_trigger_enabled_override     = var.exercise_load_triggers_enabled", main)
        self.assertIn("reconciler_trigger_enabled          = false", main)
        self.assertIn('default     = "apcf-config-dev"', variables)
        self.assertIn('condition     = var.runtime_artifact_bucket_name == "apcf-config-dev"', variables)
        self.assertIn("deployment_id: preflight", deployment)
        self.assertIn("config_bucket_name: apcf-config-preflight-667653114001", deployment)
        self.assertIn('channel_label: "#aws-change-alerts-preflight"', deployment)
        self.assertIn("credential_secret_id: preflight/slack/private-test-webhook", deployment)
        self.assertEqual(central_lambda.count("s3_bucket         = local.runtime_artifact_bucket_name"), 4)
        self.assertIn("CONFIG_BUCKET_NAME                 = aws_s3_bucket.config.id", central_lambda)
        self.assertIn("external runtime artifacts and individual trigger overrides are restricted", central_s3)
        self.assertIn('condition     = !var.preflight_mode || local.deployment_id == "preflight"', central_s3)

    def test_terraform_check_skips_an_absent_optional_binary(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_terraform = Path(temporary_directory) / "terraform"
            checked = subprocess.run(
                (
                    "make",
                    "terraform-check",
                    f"TERRAFORM={missing_terraform}",
                    "REQUIRE_TERRAFORM=0",
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(checked.returncode, 0, msg=f"{checked.stdout}\n{checked.stderr}")
            self.assertIn("terraform not installed; skipping terraform-check", checked.stdout)

    def test_terraform_check_fails_when_the_required_binary_is_absent(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_terraform = Path(temporary_directory) / "terraform"
            checked = subprocess.run(
                (
                    "make",
                    "terraform-check",
                    f"TERRAFORM={missing_terraform}",
                    "REQUIRE_TERRAFORM=1",
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(checked.returncode, 0, msg=f"{checked.stdout}\n{checked.stderr}")
            self.assertIn("terraform is required but not installed", checked.stderr)

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
        mapping = self.resource_block(lambda_source, "aws_lambda_event_source_mapping", "slack_worker")
        self.assertIn("count = local.worker_runtime_enabled ? 1 : 0", mapping)
        self.assertIn("enabled                            = local.worker_trigger_enabled", mapping)
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

    def test_application_artifact_retirement_is_a_separate_exact_prefix_role(self):
        iam = (ROOT / "infra/central/iam.tf").read_text(encoding="utf-8")
        outputs = (ROOT / "infra/central/outputs.tf").read_text(encoding="utf-8")
        trust = iam[iam.index('data "aws_iam_policy_document" "publisher_assume_role"') :]
        trust = trust[: trust.index('\nresource "aws_iam_role"', 1)]
        role = iam[iam.index('resource "aws_iam_role" "application_artifact_retirement"') :]
        role = role[: role.index("\n}") + 2]
        retirement = iam[iam.index('data "aws_iam_policy_document" "application_artifact_retirement"') :]
        retirement = retirement[: retirement.index('\ndata "aws_iam_policy_document"', 1)]

        self.assertIn('actions = ["sts:AssumeRole"]', trust)
        self.assertIn('type        = "AWS"', trust)
        self.assertIn(
            'identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]',
            trust,
        )
        self.assertIn("assume_role_policy = data.aws_iam_policy_document.publisher_assume_role.json", role)
        self.assertIn('actions   = ["s3:ListBucketVersions"]', retirement)
        self.assertIn('variable = "s3:prefix"', retirement)
        self.assertIn('values   = ["${local.application_artifact_prefix}/*"]', retirement)
        self.assertIn('actions   = ["s3:GetObjectVersion", "s3:DeleteObjectVersion"]', retirement)
        self.assertIn("${aws_s3_bucket.config.arn}/${local.application_artifact_prefix}/*", retirement)
        self.assertNotIn('s3:DeleteObject"', retirement)
        self.assertIn(
            "application_artifact_retirement = aws_iam_role.application_artifact_retirement.arn",
            outputs,
        )

    def test_publisher_and_runtime_roles_cannot_retire_application_artifacts(self):
        iam = (ROOT / "infra/central/iam.tf").read_text(encoding="utf-8")
        retirement_start = iam.index('data "aws_iam_policy_document" "application_artifact_retirement"')
        retirement_end = iam.index('\ndata "aws_iam_policy_document"', retirement_start + 1)
        without_retirement_policy = iam[:retirement_start] + iam[retirement_end:]
        self.assertNotIn("s3:DeleteObjectVersion", without_retirement_policy)

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
        self.assertIn('state               = local.watcher_trigger_enabled ? "ENABLED" : "DISABLED"', watcher_rule)

    def test_watcher_schedule_transport_uses_exact_runtime_enablement_condition(self):
        lambda_source = (ROOT / "infra/central/lambda.tf").read_text(encoding="utf-8")
        condition = "count = local.watcher_runtime_enabled ? 1 : 0"

        for resource_type, resource_name in (
            ("aws_cloudwatch_event_target", "watcher"),
            ("aws_lambda_permission", "watcher_schedule"),
        ):
            with self.subTest(resource=resource_name):
                block = self.resource_block(lambda_source, resource_type, resource_name)
                uncommented_block = re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL)
                self.assertRegex(
                    uncommented_block,
                    rf"(?m)^[ \t]*{re.escape(condition)}(?:[ \t]*(?:#|//).*)?$",
                )

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

        dispatcher_rule = self.resource_block(lambda_source, "aws_cloudwatch_event_rule", "dispatcher")
        self.assertIn(
            'state               = local.dispatcher_trigger_enabled ? "ENABLED" : "DISABLED"',
            dispatcher_rule,
        )

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

    def test_runtime_trigger_gate_validations_execute_in_provider_free_plans(self):
        variables = (ROOT / "infra/central/variables.tf").read_text(encoding="utf-8")
        selected_variables = "\n\n".join(
            self.variable_block(variables, name)
            for name in (
                "worker_artifact_sha256",
                "worker_artifact_version_id",
                "watcher_artifact_sha256",
                "watcher_artifact_version_id",
                "dispatcher_artifact_sha256",
                "dispatcher_artifact_version_id",
                "reconciler_artifact_sha256",
                "reconciler_artifact_version_id",
                "delivery_triggers_enabled",
                "reconciler_trigger_enabled",
            )
        )
        digest = "a" * 64
        delivery_artifacts = (
            f"worker_artifact_sha256={digest}",
            "worker_artifact_version_id=version-1",
            f"watcher_artifact_sha256={digest}",
            "watcher_artifact_version_id=version-1",
            f"dispatcher_artifact_sha256={digest}",
            "dispatcher_artifact_version_id=version-1",
        )
        cases = (
            ("default off", (), None, 0, None, "delivery_gate = false"),
            (
                "delivery explicit null uses default off",
                (),
                "null-delivery.tfvars.json",
                0,
                None,
                "delivery_gate = false",
            ),
            (
                "delivery gate without artifacts",
                ("delivery_triggers_enabled=true",),
                None,
                1,
                "delivery_triggers_enabled requires complete worker, watcher, and dispatcher artifact pairs.",
                None,
            ),
            (
                "delivery gate with exact cohort",
                (*delivery_artifacts, "delivery_triggers_enabled=true"),
                None,
                0,
                None,
                "delivery_gate = true",
            ),
            (
                "delivery explicit null with exact cohort stays off",
                delivery_artifacts,
                "null-delivery.tfvars.json",
                0,
                None,
                "delivery_gate = false",
            ),
            (
                "reconciler gate without artifact",
                ("reconciler_trigger_enabled=true",),
                None,
                1,
                "reconciler_trigger_enabled requires a complete reconciler artifact pair.",
                None,
            ),
            (
                "reconciler gate with artifact",
                (
                    f"reconciler_artifact_sha256={digest}",
                    "reconciler_artifact_version_id=version-1",
                    "reconciler_trigger_enabled=true",
                ),
                None,
                0,
                None,
                "reconciler_gate = true",
            ),
            (
                "reconciler explicit null with artifact stays off",
                (
                    f"reconciler_artifact_sha256={digest}",
                    "reconciler_artifact_version_id=version-1",
                ),
                "null-reconciler.tfvars.json",
                0,
                None,
                "reconciler_gate = false",
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tf").write_text(
                f'{selected_variables}\n\noutput "delivery_gate" {{\n  value = var.delivery_triggers_enabled\n}}\n'
                'output "reconciler_gate" {\n  value = var.reconciler_trigger_enabled\n}\n',
                encoding="utf-8",
            )
            (root / "null-delivery.tfvars.json").write_text('{"delivery_triggers_enabled": null}\n', encoding="utf-8")
            (root / "null-reconciler.tfvars.json").write_text(
                '{"reconciler_trigger_enabled": null}\n', encoding="utf-8"
            )
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

            for name, values, var_file, expected_exit, expected_message, expected_output in cases:
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
                    if var_file is not None:
                        command.append(f"-var-file={var_file}")
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
                    if expected_output is not None:
                        normalized_output = re.sub(r"\s+", " ", output)
                        self.assertIn(expected_output, normalized_output)

    def test_runtime_trigger_states_are_separate_from_resource_deployment(self):
        variables = (ROOT / "infra/central/variables.tf").read_text(encoding="utf-8")
        locals_source = (ROOT / "infra/central/locals.tf").read_text(encoding="utf-8")
        lambda_source = (ROOT / "infra/central/lambda.tf").read_text(encoding="utf-8")
        alarms = (ROOT / "infra/central/alarms.tf").read_text(encoding="utf-8")
        queue = (ROOT / "infra/central/sqs.tf").read_text(encoding="utf-8")
        outputs = (ROOT / "infra/central/outputs.tf").read_text(encoding="utf-8")

        for name in ("delivery_triggers_enabled", "reconciler_trigger_enabled"):
            with self.subTest(variable=name):
                block = self.variable_block(variables, name)
                self.assertIn("type        = bool", block)
                self.assertIn("default     = false", block)
                self.assertIn("nullable    = false", block)

        for runtime in ("watcher", "dispatcher", "worker"):
            with self.subTest(delivery_trigger=runtime):
                self.assertIn(
                    f"{runtime}_trigger_enabled",
                    locals_source,
                )
                self.assertIn(
                    f"var.{runtime}_trigger_enabled_override == null ? var.delivery_triggers_enabled : var.{runtime}_trigger_enabled_override",
                    locals_source,
                )
                self.assertIn(
                    f"local.{runtime}_runtime_enabled && local.{runtime}_trigger_requested",
                    locals_source,
                )
        self.assertIn(
            "reconciler_trigger_enabled        = local.reconciler_runtime_enabled && var.reconciler_trigger_enabled",
            locals_source,
        )

        for runtime in ("watcher", "dispatcher"):
            with self.subTest(schedule=runtime):
                rule = self.resource_block(lambda_source, "aws_cloudwatch_event_rule", runtime)
                self.assertIn(f"count = local.{runtime}_runtime_enabled ? 1 : 0", rule)
                self.assertIn(
                    f'state               = local.{runtime}_trigger_enabled ? "ENABLED" : "DISABLED"',
                    rule,
                )

        reconciler_rule = self.resource_block(lambda_source, "aws_cloudwatch_event_rule", "reconciler")
        self.assertNotIn("count =", reconciler_rule)
        self.assertIn(
            'state               = local.reconciler_trigger_enabled ? "ENABLED" : "DISABLED"',
            reconciler_rule,
        )
        self.assertIn("for_each = local.watcher_runtime_enabled ? [1] : []", queue)
        self.assertIn("for_each = local.dispatcher_runtime_enabled ? [1] : []", queue)

        output = outputs[outputs.index('output "runtime_trigger_states"') :]
        output = output[: output.index("\noutput ", 1)]
        for runtime in ("watcher", "dispatcher", "worker", "reconciler"):
            with self.subTest(output=runtime):
                self.assertIn(f"{runtime}", output)
                self.assertIn(f"local.{runtime}_trigger_enabled", output)

        for alarm in (
            "feed_watcher_errors",
            "watcher_incomplete_runs",
            "watcher_fault",
            "feed_watcher_heartbeat",
            "dispatcher_errors",
            "dispatcher_heartbeat",
            "reconciler_heartbeat",
        ):
            with self.subTest(alarm=alarm):
                block = self.resource_block(alarms, "aws_cloudwatch_metric_alarm", alarm)
                runtime = "watcher" if alarm.startswith(("feed_watcher", "watcher_")) else alarm.split("_", 1)[0]
                self.assertIn(f"count = local.{runtime}_trigger_enabled ? 1 : 0", block)

        for alarm in ("delivery_queue_age", "delivery_dlq_depth", "worker_errors", "delivery_unknown"):
            with self.subTest(always_observed_alarm=alarm):
                block = self.resource_block(alarms, "aws_cloudwatch_metric_alarm", alarm)
                self.assertNotIn("count = local.", block)

    def test_runtime_trigger_rollout_docs_expose_staging_limits(self):
        specification = (ROOT / "docs/architecture/specification/05-security-and-operations.md").read_text(
            encoding="utf-8"
        )
        runbook = (ROOT / "docs/runbooks/operations.md").read_text(encoding="utf-8")
        terraform_sources = self.terraform_root_sources()
        specification = re.sub(r"\s+", " ", specification)
        rollout = runbook[runbook.index("## Application package rollout and rollback") :]
        rollout = rollout[: rollout.index("\n## Manual source replay")]
        step_6 = rollout[rollout.index("\n6. ") : rollout.index("\n7. ")]
        rollout = re.sub(r"\s+", " ", rollout)

        for required in (
            "`scripts/preflight_delivery.py preview` binds the reviewed dev deployment",
            "`candidate_cap_exceeded` and stops before dispatch.",
            "`WatcherFaults`, only when the watcher runtime is enabled.",
            "`IncompleteRuns`, only when the watcher runtime is enabled.",
        ):
            with self.subTest(specification=required):
                self.assertIn(required, specification)

        for required in (
            "python3 scripts/preflight_delivery.py preview",
            "--deployment infra/central/deployment.yaml",
            "`no_positive_match` ends the attempt without extending the sample.",
            "paging from those alarms begins after step 7 enables the delivery cohort",
        ):
            with self.subTest(runbook=required):
                self.assertIn(required, rollout)

        self.assert_delivery_trigger_alarm_inventory_matches_runbook(terraform_sources, step_6)

    def test_rollout_alarm_inventory_rejects_source_and_runbook_drift(self):
        terraform_sources = self.terraform_root_sources()
        alarms = terraform_sources["alarms.tf"]
        runbook = (ROOT / "docs/runbooks/operations.md").read_text(encoding="utf-8")
        rollout = runbook[runbook.index("## Application package rollout and rollback") :]
        step_6 = rollout[rollout.index("\n6. ") : rollout.index("\n7. ")]
        undocumented_watcher_alarm = """
resource "aws_cloudwatch_metric_alarm" "undocumented_delivery_gate" {
  count = local.watcher_trigger_enabled ? 1 : 0
  alarm_name = "apcf-${local.deployment_id}-undocumented-delivery-gate"
}
"""
        undocumented_worker_alarm = undocumented_watcher_alarm.replace(
            "local.watcher_trigger_enabled", "local.worker_trigger_enabled"
        )
        trailing_comment_alarm = undocumented_watcher_alarm.replace(
            " {\n",
            " { # formatted source comment\n",
            1,
        )
        for_each_alarm = undocumented_watcher_alarm.replace(
            "count = local.watcher_trigger_enabled ? 1 : 0",
            'for_each = local.watcher_trigger_enabled ? toset(["1"]) : toset([])',
        )
        one_hop_alias_alarm = undocumented_watcher_alarm.replace(
            "count = local.watcher_trigger_enabled ? 1 : 0",
            "count = local.watcher_gate_alias ? 1 : 0",
        )
        two_hop_alias_alarm = undocumented_watcher_alarm.replace(
            "count = local.watcher_trigger_enabled ? 1 : 0",
            "count = local.watcher_gate_alias_2 ? 1 : 0",
        )
        direct_variable_alarm = undocumented_watcher_alarm.replace(
            "count = local.watcher_trigger_enabled ? 1 : 0",
            "count = var.delivery_triggers_enabled ? 1 : 0",
        )
        multiline_alarm = undocumented_watcher_alarm.replace(
            "count = local.watcher_trigger_enabled ? 1 : 0",
            "count = (\n    local.watcher_trigger_enabled ? 1 : 0\n  )",
        )
        inverted_count_alarm = undocumented_watcher_alarm.replace(
            "count = local.watcher_trigger_enabled ? 1 : 0",
            "count = local.watcher_trigger_enabled ? 0 : 1",
        )
        documented_gate_pair = (
            "`aws_cloudwatch_metric_alarm.undocumented_delivery_gate` (`apcf-<deployment>-undocumented-delivery-gate`)"
        )
        documented_gate_step = f"{step_6} {documented_gate_pair}"
        canonical_worker_sources = dict(terraform_sources)
        canonical_worker_sources["alarms.tf"] = f"{alarms}\n{undocumented_worker_alarm}"
        self.assert_delivery_trigger_alarm_inventory_matches_runbook(
            canonical_worker_sources,
            documented_gate_step,
        )

        stale_runbook = (
            f"{step_6} `aws_cloudwatch_metric_alarm.documented_only_gate` (`apcf-<deployment>-documented-only-gate`)"
        )
        removed_alarm = step_6.replace(
            "`aws_cloudwatch_metric_alarm.watcher_fault` (`apcf-<deployment>-watcher-fault`), ",
            "",
        )
        wrong_cloudwatch_name = step_6.replace(
            "`apcf-<deployment>-outbox-dispatcher-heartbeat`",
            "`apcf-<deployment>-dispatcher-heartbeat`",
        )
        watcher_fault_pair = "`aws_cloudwatch_metric_alarm.watcher_fault` (`apcf-<deployment>-watcher-fault`)"
        duplicate_runbook_entry = step_6.replace(
            watcher_fault_pair,
            "`aws_cloudwatch_metric_alarm.watcher_fault` (`apcf-<deployment>-wrong-watcher-fault`), "
            f"{watcher_fault_pair}",
        )
        unparsed_runbook_pair = step_6.replace(
            watcher_fault_pair,
            "`aws_cloudwatch_metric_alarm.watcher_fault` (`apcf-<deployment>-WRONG-watcher-fault`), "
            f"{watcher_fault_pair}",
        )
        reconciler_overdocumented = (
            f"{step_6} `aws_cloudwatch_metric_alarm.reconciler_heartbeat` "
            "(`apcf-<deployment>-recovery-reconciler-heartbeat`)"
        )

        def sources_with_alarm(alarm_source, file_name="alarms.tf"):
            result = dict(terraform_sources)
            result[file_name] = f"{result.get(file_name, '')}\n{alarm_source}"
            return result

        for name, sources, runbook_step in (
            ("new watcher alarm", sources_with_alarm(undocumented_watcher_alarm), step_6),
            ("new worker alarm", sources_with_alarm(undocumented_worker_alarm), step_6),
            ("trailing declaration comment", sources_with_alarm(trailing_comment_alarm), step_6),
            ("for_each selector", sources_with_alarm(for_each_alarm), documented_gate_step),
            ("one-hop alias", sources_with_alarm(one_hop_alias_alarm), documented_gate_step),
            ("two-hop alias", sources_with_alarm(two_hop_alias_alarm), documented_gate_step),
            ("direct variable", sources_with_alarm(direct_variable_alarm), documented_gate_step),
            ("multiline selector", sources_with_alarm(multiline_alarm), documented_gate_step),
            ("inverted count", sources_with_alarm(inverted_count_alarm), documented_gate_step),
            (
                "alarm in a second root file",
                sources_with_alarm(undocumented_watcher_alarm, "extra_alarms.tf"),
                documented_gate_step,
            ),
            ("stale runbook entry", terraform_sources, stale_runbook),
            ("removed runbook entry", terraform_sources, removed_alarm),
            ("wrong CloudWatch name", terraform_sources, wrong_cloudwatch_name),
            ("duplicate runbook entry", terraform_sources, duplicate_runbook_entry),
            ("unparsed runbook pair", terraform_sources, unparsed_runbook_pair),
            ("reconciler overdocumented", terraform_sources, reconciler_overdocumented),
        ):
            with (
                self.subTest(variant=name),
                self.assertRaisesRegex(
                    AssertionError,
                    "root CloudWatch alarm|runbook step 6",
                ),
            ):
                self.assert_delivery_trigger_alarm_inventory_matches_runbook(sources, runbook_step)

    def test_dispatcher_errors_and_heartbeat_follow_trigger_enablement(self):
        alarms = (ROOT / "infra/central/alarms.tf").read_text(encoding="utf-8")
        lambda_source = (ROOT / "infra/central/lambda.tf").read_text(encoding="utf-8")
        function = self.resource_block(lambda_source, "aws_lambda_function", "dispatcher")
        self.assertIn("count = local.dispatcher_runtime_enabled ? 1 : 0", function)

        for name in ("dispatcher_errors", "dispatcher_heartbeat"):
            with self.subTest(resource=name):
                self.assertIn(
                    "count = local.dispatcher_trigger_enabled ? 1 : 0",
                    self.resource_block(alarms, "aws_cloudwatch_metric_alarm", name),
                )

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
        self.assertIn("count = local.watcher_trigger_enabled ? 1 : 0", heartbeat)

    def test_publisher_and_watcher_list_only_the_active_manifest_key(self):
        iam = (ROOT / "infra/central/iam.tf").read_text(encoding="utf-8")
        publisher = iam[iam.index('data "aws_iam_policy_document" "release_publisher"') :]
        publisher = publisher[: publisher.index('\ndata "aws_iam_policy_document"', 1)]
        watcher = iam[iam.index('data "aws_iam_policy_document" "feed_watcher"') :]
        watcher = watcher[: watcher.index('\ndata "aws_iam_policy_document"', 1)]

        for name, policy in (("publisher", publisher), ("watcher", watcher)):
            with self.subTest(role=name):
                start = policy.index('sid       = "ListActiveManifestKey"')
                end = policy.index("\n  statement {", start)
                statement = policy[start:end]
                self.assertIn('actions   = ["s3:ListBucket"]', statement)
                self.assertIn("resources = [aws_s3_bucket.config.arn]", statement)
                self.assertIn('test     = "StringEquals"', statement)
                self.assertIn('variable = "s3:prefix"', statement)
                self.assertIn("values   = [local.active_versions_key]", statement)
                self.assertIn('test     = "NumericLessThanEquals"', statement)
                self.assertIn('variable = "s3:max-keys"', statement)
                self.assertIn('values   = ["1"]', statement)
                self.assertEqual(statement.count("s3:ListBucket"), 1)
                self.assertEqual(policy.count('"s3:ListBucket"'), 1)
                self.assertNotIn('"s3:ListBucketVersions"', policy)

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

        self.assertIn("count = local.reconciler_runtime_enabled ? 1 : 0", reconciler)
        self.assertIn("count = local.reconciler_trigger_enabled ? 1 : 0", heartbeat)

    def test_watcher_failure_alarms_match_the_accepted_paging_policy(self):
        alarms = (ROOT / "infra/central/alarms.tf").read_text(encoding="utf-8")
        lambda_source = (ROOT / "infra/central/lambda.tf").read_text(encoding="utf-8")
        watcher = self.resource_block(lambda_source, "aws_lambda_function", "watcher")
        errors = self.resource_block(alarms, "aws_cloudwatch_metric_alarm", "feed_watcher_errors")
        incomplete = self.resource_block(alarms, "aws_cloudwatch_metric_alarm", "watcher_incomplete_runs")
        fault = self.resource_block(alarms, "aws_cloudwatch_metric_alarm", "watcher_fault")

        self.assertIn("count = local.watcher_runtime_enabled ? 1 : 0", watcher)
        for resource, block in (
            ("watcher_errors", errors),
            ("watcher_incomplete_runs", incomplete),
            ("watcher_fault", fault),
        ):
            with self.subTest(resource=resource):
                self.assertIn("count = local.watcher_trigger_enabled ? 1 : 0", block)

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

        self.assertIn("evaluation_periods  = 1", fault)
        self.assertIn("datapoints_to_alarm = 1", fault)
        self.assertIn("period              = 300", fault)
        self.assertIn('metric_name         = "WatcherFaults"', fault)
        self.assertIn("namespace           = local.metrics_namespace", fault)
        self.assertIn("Function = local.function_names.watcher", fault)
        self.assertIn('treat_missing_data  = "notBreaching"', fault)
        self.assertIn("alarm_actions             = [aws_sns_topic.operations.arn]", fault)

        release_verification = self.resource_block(
            alarms, "aws_cloudwatch_metric_alarm", "release_verification_failures"
        )
        self.assertIn('metric_name         = "ReleaseVerificationFailures"', release_verification)
        self.assertNotIn("alarm_actions", release_verification)
        self.assertNotIn("ok_actions", release_verification)
        self.assertNotIn("insufficient_data_actions", release_verification)
        self.assertIn("Diagnostic only; WatcherFaults owns notification paging", release_verification)

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
