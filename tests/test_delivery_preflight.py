from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("preflight_delivery", ROOT / "scripts/preflight_delivery.py")
assert SPEC is not None
preflight = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preflight
assert SPEC.loader is not None
SPEC.loader.exec_module(preflight)

from aws_public_change_feed import dispatcher_runtime, slack_worker_runtime, watcher_runtime  # noqa: E402

AS_OF = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


class FakeSqs:
    def __init__(self, *, delete_error: Exception | None = None) -> None:
        self.delete_error = delete_error
        self.deleted: list[dict[str, str]] = []

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error


class FakeStore:
    def __init__(self, states: list[str]) -> None:
        self.states = list(states)

    def get_delivery(self, candidate_id):
        del candidate_id
        state = self.states.pop(0)
        return SimpleNamespace(status=state, queue_message_id="message-1")


class PreviewFixture:
    def __init__(self, directory: str) -> None:
        self.deployment_path = ROOT / "infra/central/deployment.yaml"
        self.config_path = ROOT / "config/dev.yaml"
        self.output_path = Path(directory) / "central-outputs.json"
        self.deployment, _ = preflight._read_mapping(self.deployment_path, "deployment")
        self.config, _ = preflight._read_mapping(self.config_path, "configuration")
        self.digest = "a" * 64
        self.application_version = f"sha256:{self.digest}"
        self.account = "123456789012"
        self.failure_queue_arn = f"arn:aws:sqs:us-east-1:{self.account}:apcf-runtime-failures-dev"
        self.delivery_queue_name = "apcf-delivery-dev.fifo"
        self.delivery_queue_arn = f"arn:aws:sqs:us-east-1:{self.account}:{self.delivery_queue_name}"
        self.names = {
            "watcher": "apcf-dev-feed-watcher",
            "dispatcher": "apcf-dev-outbox-dispatcher",
            "worker": "apcf-dev-slack-worker",
        }
        self.roles = {
            "feed_watcher": f"arn:aws:iam::{self.account}:role/watcher",
            "outbox_dispatcher": f"arn:aws:iam::{self.account}:role/dispatcher",
            "slack_worker": f"arn:aws:iam::{self.account}:role/worker",
        }
        self.outputs = {
            "function_names": self.output(self.names),
            "roles": self.output(self.roles),
            "runtime_trigger_states": self.output(
                {"watcher": False, "dispatcher": False, "worker": False, "reconciler": False}
            ),
            "worker_application_version": self.output(self.application_version),
            "watcher_application_version": self.output(self.application_version),
            "dispatcher_application_version": self.output(self.application_version),
            "config_bucket_name": self.output(self.deployment["config_bucket_name"]),
            "delivery_table": self.output("apcf-delivery-dev"),
            "delivery_index_name": self.output("status-next-action-index"),
            "delivery_queue": self.output(self.delivery_queue_name),
            "delivery_queue_arn": self.output(self.delivery_queue_arn),
            "runtime_failure_queue": self.output("apcf-runtime-failures-dev"),
        }
        self.inventory = {
            "schema_version": 3,
            "deployment_id": self.deployment["deployment_id"],
            "generated_at": "2026-08-25T12:00:00Z",
            "deployment_region": self.deployment["deployment_region"],
            "slack": self.deployment["slack"],
            "environments": [
                {key: environment[key] for key in ("id", "customer", "account_id", "regions", "route_id")}
                for environment in self.deployment["environments"]
            ],
        }
        self.reference = {
            "release_id": "release-1",
            "config": {
                "key": "apcf/releases/config.yaml",
                "version_id": "config-version",
                "sha256": "b" * 64,
                "schema_version": 4,
            },
            "inventory": {
                "key": "apcf/releases/inventory.json",
                "version_id": "inventory-version",
                "sha256": "c" * 64,
                "schema_version": 3,
            },
            "application_version": self.application_version,
        }
        self.release = SimpleNamespace(
            release_id="release-1",
            reference=self.reference,
            config=self.config,
            inventory=self.inventory,
        )
        self.function_configurations = {
            name: {
                "FunctionName": function_name,
                "FunctionArn": f"arn:aws:lambda:us-east-1:{self.account}:function:{function_name}",
                "State": "Active",
                "LastUpdateStatus": "Successful",
                "Runtime": "python3.12",
                "Handler": preflight.EXPECTED_HANDLERS[name],
                "Role": self.roles[
                    {"watcher": "feed_watcher", "dispatcher": "outbox_dispatcher", "worker": "slack_worker"}[name]
                ],
                "CodeSha256": preflight._code_sha256(self.digest),
                "Timeout": preflight.EXPECTED_TIMEOUTS[name],
                "Environment": {
                    "Variables": {"APPLICATION_VERSION": self.application_version}
                    if name in ("watcher", "worker")
                    else {}
                },
            }
            for name, function_name in self.names.items()
        }
        self.rules = {
            name: {
                "Name": function_name,
                "Arn": f"arn:aws:events:us-east-1:{self.account}:rule/{function_name}",
                "State": "DISABLED",
                "ScheduleExpression": preflight.EXPECTED_SCHEDULES[name]["expression"],
            }
            for name, function_name in self.names.items()
            if name in ("watcher", "dispatcher")
        }
        self.mapping = {
            "UUID": "mapping-1",
            "State": "Disabled",
            "BatchSize": 10,
            "MaximumBatchingWindowInSeconds": 0,
            "ScalingConfig": {"MaximumConcurrency": 2},
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
        }
        self.delivery_attributes = {
            "QueueArn": self.delivery_queue_arn,
            "FifoQueue": "true",
            "ContentBasedDeduplication": "false",
            "VisibilityTimeout": "1800",
            "ApproximateNumberOfMessages": "0",
            "ApproximateNumberOfMessagesNotVisible": "0",
        }
        self.store = Mock()
        self.store.query_state.return_value = ()
        self.clients = self.clients_for_state()

    @staticmethod
    def output(value):
        return {"sensitive": False, "type": "test", "value": value}

    def clients_for_state(self):
        sts = Mock(spec=["get_caller_identity"])
        sts.get_caller_identity.return_value = {
            "Account": self.account,
            "Arn": f"arn:aws:iam::{self.account}:role/operator",
        }
        lambda_client = Mock(
            spec=[
                "get_function_configuration",
                "get_function_concurrency",
                "list_event_source_mappings",
                "invoke",
            ]
        )
        lambda_client.get_function_configuration.side_effect = lambda FunctionName: self.function_configurations[
            next(name for name, value in self.names.items() if value == FunctionName)
        ]
        lambda_client.get_function_concurrency.side_effect = lambda FunctionName: {
            "ReservedConcurrentExecutions": 2 if FunctionName == self.names["worker"] else 1
        }
        lambda_client.list_event_source_mappings.return_value = {"EventSourceMappings": [self.mapping]}
        events = Mock(spec=["describe_rule", "list_targets_by_rule"])
        events.describe_rule.side_effect = lambda Name: self.rules[
            next(name for name, value in self.names.items() if value == Name)
        ]

        def targets(*, Rule, Limit):
            del Limit
            name = next(name for name, value in self.names.items() if value == Rule)
            expected = preflight.EXPECTED_SCHEDULES[name]
            return {
                "Targets": [
                    {
                        "Arn": self.function_configurations[name]["FunctionArn"],
                        "RetryPolicy": {
                            "MaximumEventAgeInSeconds": expected["maximum_event_age"],
                            "MaximumRetryAttempts": expected["maximum_retries"],
                        },
                        "DeadLetterConfig": {"Arn": self.failure_queue_arn},
                    }
                ]
            }

        events.list_targets_by_rule.side_effect = targets
        sqs = Mock(spec=["get_queue_url", "get_queue_attributes", "receive_message", "delete_message"])
        sqs.get_queue_url.side_effect = lambda QueueName: {"QueueUrl": f"https://sqs.example/{QueueName}"}

        def queue_attributes(*, QueueUrl, AttributeNames):
            del AttributeNames
            name = QueueUrl.rsplit("/", 1)[-1]
            if name == self.delivery_queue_name:
                return {"Attributes": self.delivery_attributes}
            return {"Attributes": {"QueueArn": self.failure_queue_arn}}

        sqs.get_queue_attributes.side_effect = queue_attributes
        secretsmanager = Mock(spec=["describe_secret", "get_secret_value"])
        credential_id = self.deployment["slack"]["routes"]["dev-alerts"]["credential_secret_id"]
        secretsmanager.describe_secret.return_value = {
            "Name": credential_id,
            "ARN": f"arn:aws:secretsmanager:us-east-1:{self.account}:secret:{credential_id}",
        }
        return preflight.AwsClients(
            sts=sts,
            lambda_client=lambda_client,
            events=events,
            sqs=sqs,
            dynamodb=Mock(),
            s3=Mock(),
            secretsmanager=secretsmanager,
            ssm=Mock(),
        )

    def run(self, *, expected_account: str | None = None, candidate_cap: int = 10):
        self.output_path.write_text(json.dumps(self.outputs), encoding="utf-8")
        with (
            patch.object(preflight, "DynamoDBDeliveryStore", return_value=self.store),
            patch.object(preflight, "load_active_release", return_value=self.release),
        ):
            return preflight.build_preview(
                self.clients,
                deployment_path=self.deployment_path,
                config_path=self.config_path,
                terraform_output_path=self.output_path,
                expected_account=expected_account or self.account,
                application_digest=self.digest,
                candidate_cap=candidate_cap,
            )


def plan(*, cap: int = 10) -> dict:
    digest = "a" * 64
    return {
        "plan_version": 1,
        "local": {
            "deployment_path": "/reviewed/deployment.yaml",
            "deployment_sha256": "b" * 64,
            "config_path": "/reviewed/config.yaml",
            "config_sha256": "c" * 64,
            "terraform_output_path": "/reviewed/outputs.json",
            "terraform_output_sha256": "d" * 64,
        },
        "aws": {
            "account": "123456789012",
            "caller_arn": "arn:aws:iam::123456789012:role/operator",
            "region": "us-east-1",
        },
        "application": {"digest": digest, "version": f"sha256:{digest}"},
        "release": {
            "id": "release-1",
            "reference": {"config": {"key": "releases/config.json"}},
            "feed_count": 4,
            "config_bucket": "apcf-config-dev",
            "pointer_key": "aws-public-change-alerting/active-versions.json",
        },
        "runtime": {
            "functions": {
                "watcher": {"name": "watcher"},
                "dispatcher": {"name": "dispatcher"},
                "worker": {"name": "worker"},
            },
            "schedules": {"watcher": {"arn": "arn:watcher"}, "dispatcher": {"arn": "arn:dispatcher"}},
        },
        "delivery": {
            "table": "delivery",
            "index": "status-next-action-index",
            "queue": {"url": "https://sqs.example/queue", "arn": "arn:queue"},
            "route_id": "shared-alerts",
            "destination_key": "shared-aws-change-alerts",
            "channel_label": "#aws-change-alerts",
        },
        "bounds": {"candidate_cap": cap, "worker_message_count": 1},
    }


def request() -> dict:
    return {
        "request_id": "request-1",
        "candidate": {
            "candidate_id": "candidate-1",
            "announcement": {"source_type": "public_feed", "url": "https://aws.amazon.com/example"},
        },
    }


class DeliveryPreflightApplyTests(unittest.TestCase):
    def run_apply(
        self,
        invoke_results,
        *,
        states=("queued", "posted"),
        candidate_ids=("candidate-1",),
        cap=10,
        delete_error=None,
    ):
        saved_plan = plan(cap=cap)
        sqs = FakeSqs(delete_error=delete_error)
        clients = SimpleNamespace(s3=object(), sqs=sqs, dynamodb=object())
        store = FakeStore(list(states))
        release = SimpleNamespace(config={"message_policy": {"max_delivery_request_bytes": 262144}})
        with (
            patch.object(preflight, "build_preview", return_value=saved_plan),
            patch.object(preflight, "DynamoDBDeliveryStore", return_value=store),
            patch.object(preflight, "load_active_release", return_value=release) as load_release,
            patch.object(preflight, "_invoke", side_effect=invoke_results) as invoke,
            patch.object(preflight, "_delivery_ids", return_value=tuple(candidate_ids)),
            patch.object(preflight, "_receive_one", return_value={"MessageId": "message-1"}),
            patch.object(preflight, "_validate_message", return_value=(request(), "message-1", "receipt-1")),
            patch.object(preflight, "_worker_event", return_value={"Records": []}),
        ):
            result = preflight.apply_plan(clients, saved_plan, clock=lambda: AS_OF)
        loaded_store = load_release.call_args.args[0]
        self.assertEqual(loaded_store._bucket, "apcf-config-dev")
        self.assertEqual(load_release.call_args.kwargs["pointer_key"], saved_plan["release"]["pointer_key"])
        return result, invoke, sqs

    def test_zero_match_stops_after_one_watcher_invocation(self):
        result, invoke, sqs = self.run_apply([({"feeds": 4, "advanced": 4, "candidates": 0}, False)], states=[])
        self.assertEqual(result, {"status": "no_positive_match", "feeds": 4, "candidates": 0})
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(sqs.deleted, [])

    def test_candidate_count_over_d0_cap_stops_before_dispatch(self):
        result, invoke, sqs = self.run_apply(
            [({"feeds": 4, "advanced": 4, "candidates": 11}, False)],
            states=[],
        )
        self.assertEqual(result["status"], "candidate_cap_exceeded")
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(sqs.deleted, [])

    def test_posted_deletes_only_the_received_receipt(self):
        result, invoke, sqs = self.run_apply(
            [
                ({"feeds": 4, "advanced": 4, "candidates": 1}, False),
                ({"considered": 1, "accepted": 1, "unknown": 0, "failed_transitions": 0}, False),
                ({"batchItemFailures": []}, False),
            ]
        )
        self.assertEqual(result["status"], "posted")
        self.assertTrue(result["operator_slack_confirmation_required"])
        self.assertEqual(invoke.call_count, 3)
        self.assertEqual(
            sqs.deleted,
            [{"QueueUrl": "https://sqs.example/queue", "ReceiptHandle": "receipt-1"}],
        )

    def test_unknown_worker_result_preserves_the_receipt(self):
        result, _, sqs = self.run_apply(
            [
                ({"feeds": 4, "advanced": 4, "candidates": 1}, False),
                ({"considered": 1, "accepted": 1, "unknown": 0, "failed_transitions": 0}, False),
                (None, True),
            ],
            states=("queued", "queued"),
        )
        self.assertEqual(result["status"], "worker_unknown")
        self.assertEqual(result["durable_state"], "queued")
        self.assertEqual(sqs.deleted, [])

    def test_delete_error_reports_unknown_after_durable_post(self):
        result, _, sqs = self.run_apply(
            [
                ({"feeds": 4, "advanced": 4, "candidates": 1}, False),
                ({"considered": 1, "accepted": 1, "unknown": 0, "failed_transitions": 0}, False),
                ({"batchItemFailures": []}, False),
            ],
            delete_error=RuntimeError("provider detail"),
        )
        self.assertEqual(result["status"], "delete_unknown")
        self.assertEqual(result["durable_state"], "posted")
        self.assertEqual(len(sqs.deleted), 1)

    def test_dispatcher_mismatch_is_refused_before_queue_receive(self):
        with self.assertRaisesRegex(preflight.PreflightError, "dispatcher result") as raised:
            self.run_apply(
                [
                    ({"feeds": 4, "advanced": 4, "candidates": 1}, False),
                    ({"considered": 1, "accepted": 0, "unknown": 1, "failed_transitions": 0}, False),
                ],
                states=(),
            )
        self.assertEqual(raised.exception.status, "dispatcher_refused")

    def test_watcher_incompletion_is_refused(self):
        with self.assertRaisesRegex(preflight.PreflightError, "every planned feed") as raised:
            self.run_apply([({"feeds": 4, "advanced": 3, "candidates": 0}, False)], states=())
        self.assertEqual(raised.exception.status, "watcher_refused")

    def test_unknown_dispatcher_result_stops_before_queue_receive(self):
        with self.assertRaisesRegex(preflight.PreflightError, "dispatcher invocation") as raised:
            self.run_apply(
                [
                    ({"feeds": 4, "advanced": 4, "candidates": 1}, False),
                    (None, True),
                ],
                states=(),
            )
        self.assertEqual(raised.exception.status, "dispatcher_unknown")

    def test_worker_refusal_preserves_the_receipt(self):
        result, _, sqs = self.run_apply(
            [
                ({"feeds": 4, "advanced": 4, "candidates": 1}, False),
                ({"considered": 1, "accepted": 1, "unknown": 0, "failed_transitions": 0}, False),
                ({"batchItemFailures": [{"itemIdentifier": "message-1"}]}, False),
            ],
            states=("queued", "queued"),
        )
        self.assertEqual(result["status"], "worker_refused")
        self.assertEqual(sqs.deleted, [])

    def test_changed_preview_is_refused_before_any_runtime_invocation(self):
        saved_plan = plan()
        changed_plan = plan()
        changed_plan["release"]["id"] = "changed"
        clients = SimpleNamespace()
        with (
            patch.object(preflight, "build_preview", return_value=changed_plan),
            patch.object(preflight, "_invoke") as invoke,
            self.assertRaisesRegex(preflight.PreflightError, "changed after preview") as raised,
        ):
            preflight.apply_plan(clients, saved_plan, clock=lambda: AS_OF)
        self.assertEqual(raised.exception.status, "stale_plan")
        invoke.assert_not_called()


class DeliveryPreflightInspectionTests(unittest.TestCase):
    def test_build_preview_runs_the_complete_read_only_path(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PreviewFixture(directory)
            result = fixture.run()

        self.assertEqual(result["aws"]["account"], fixture.account)
        self.assertEqual(result["application"]["version"], fixture.application_version)
        self.assertEqual(result["release"]["reference"], fixture.reference)
        self.assertEqual(result["bounds"], {"candidate_cap": 10, "worker_message_count": 1})
        self.assertEqual(result["delivery"]["route_id"], "dev-alerts")
        self.assertEqual(result["delivery"]["channel_label"], "#aws-change-alerts-dev")
        fixture.clients.lambda_client.invoke.assert_not_called()
        fixture.clients.sqs.receive_message.assert_not_called()
        fixture.clients.sqs.delete_message.assert_not_called()
        fixture.clients.secretsmanager.get_secret_value.assert_not_called()

    def test_build_preview_refuses_each_identity_and_state_partition(self):
        for variant in (
            "account",
            "trigger",
            "function",
            "schedule",
            "mapping",
            "queue",
            "actionable",
            "inventory",
            "credential",
        ):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as directory:
                fixture = PreviewFixture(directory)
                expected_status = "state_refused"
                expected_account = None
                if variant == "account":
                    expected_status = "identity_refused"
                    expected_account = "999999999999"
                elif variant == "trigger":
                    fixture.outputs["runtime_trigger_states"]["value"]["watcher"] = True
                elif variant == "function":
                    fixture.function_configurations["watcher"]["Handler"] = "wrong.handler"
                elif variant == "schedule":
                    fixture.rules["dispatcher"]["State"] = "ENABLED"
                elif variant == "mapping":
                    fixture.mapping["State"] = "Enabled"
                elif variant == "queue":
                    fixture.delivery_attributes["ApproximateNumberOfMessages"] = "1"
                elif variant == "actionable":
                    fixture.store.query_state.return_value = ((1, "candidate-1"),)
                elif variant == "inventory":
                    fixture.inventory["deployment_id"] = "other"
                else:
                    fixture.clients.secretsmanager.describe_secret.return_value["Name"] = "other"

                with self.assertRaises(preflight.PreflightError) as raised:
                    fixture.run(expected_account=expected_account)
                self.assertEqual(raised.exception.status, expected_status)
                fixture.clients.lambda_client.invoke.assert_not_called()

    def test_preview_requires_the_exact_d0_candidate_cap(self):
        for candidate_cap in (1, 9, 11):
            with self.subTest(candidate_cap=candidate_cap), tempfile.TemporaryDirectory() as directory:
                fixture = PreviewFixture(directory)
                with self.assertRaisesRegex(preflight.PreflightError, "must equal 10") as raised:
                    fixture.run(candidate_cap=candidate_cap)
                self.assertEqual(raised.exception.status, "invalid_input")
                fixture.clients.sts.get_caller_identity.assert_not_called()

    def test_generated_schedule_and_fifo_envelopes_satisfy_the_runtime_validators(self):
        saved_plan = plan()
        for runtime, validator in (
            ("watcher", watcher_runtime._validate_schedule_event),
            ("dispatcher", dispatcher_runtime._validate_schedule_event),
        ):
            with self.subTest(runtime=runtime):
                event = preflight._schedule_event(saved_plan, runtime, AS_OF)
                validator(event)
                self.assertEqual(event["resources"], [saved_plan["runtime"]["schedules"][runtime]["arn"]])

        delivery_request = json.loads((ROOT / "examples/delivery-request.json").read_text(encoding="utf-8"))
        message = {
            "MessageId": "message-1",
            "ReceiptHandle": "receipt-1",
            "Body": json.dumps(delivery_request),
            "Attributes": {"MessageGroupId": "destination-1"},
        }
        event = preflight._worker_event(message, "arn:queue", "us-east-1")
        candidate_id, delivery = slack_worker_runtime._delivery_from_record(
            event["Records"][0],
            max_delivery_request_bytes=245760,
        )
        self.assertEqual(candidate_id, delivery_request["candidate"]["candidate_id"])
        self.assertEqual(delivery.message_id, "message-1")
        self.assertEqual(delivery.message_group_id, "destination-1")

    def test_queued_message_validation_uses_the_real_contract_and_release(self):
        config, _ = preflight._read_mapping(ROOT / "examples/config.yaml", "configuration")
        inventory, _ = preflight._read_mapping(ROOT / "examples/inventory.json", "inventory")
        delivery_request = json.loads((ROOT / "examples/delivery-request.json").read_text(encoding="utf-8"))
        candidate = delivery_request["candidate"]
        saved_plan = plan()
        saved_plan["release"]["reference"] = candidate["release"]
        saved_plan["delivery"]["destination_key"] = delivery_request["destination_key"]
        release = SimpleNamespace(config=config, inventory=inventory)
        message = {
            "MessageId": "message-1",
            "ReceiptHandle": "receipt-1",
            "Body": json.dumps(delivery_request),
        }
        request_result, message_id, receipt = preflight._validate_message(
            message,
            plan=saved_plan,
            release=release,
            candidate_ids=(candidate["candidate_id"],),
        )
        self.assertEqual(request_result, delivery_request)
        self.assertEqual((message_id, receipt), ("message-1", "receipt-1"))

        with self.assertRaisesRegex(preflight.PreflightError, "not created by this preflight") as raised:
            preflight._validate_message(
                message,
                plan=saved_plan,
                release=release,
                candidate_ids=("other",),
            )
        self.assertEqual(raised.exception.status, "message_refused")

    def test_missing_received_message_is_a_bounded_refusal(self):
        sqs = Mock()
        sqs.receive_message.return_value = {"Messages": []}
        with self.assertRaisesRegex(preflight.PreflightError, "exactly one") as raised:
            preflight._receive_one(SimpleNamespace(sqs=sqs), "https://sqs.example/queue")
        self.assertEqual(raised.exception.status, "queue_not_observed")

    def test_schedule_binds_disabled_cadence_retry_and_failure_queue(self):
        function = {"name": "watcher", "arn": "arn:function"}
        events = Mock()
        events.describe_rule.return_value = {
            "Name": "watcher",
            "Arn": "arn:rule",
            "State": "DISABLED",
            "ScheduleExpression": "rate(15 minutes)",
        }
        events.list_targets_by_rule.return_value = {
            "Targets": [
                {
                    "Arn": "arn:function",
                    "RetryPolicy": {"MaximumEventAgeInSeconds": 900, "MaximumRetryAttempts": 2},
                    "DeadLetterConfig": {"Arn": "arn:failure"},
                }
            ]
        }
        result = preflight._inspect_schedule(
            SimpleNamespace(events=events),
            name="watcher",
            function=function,
            failure_queue_arn="arn:failure",
        )
        self.assertEqual(result["state"], "DISABLED")
        events.describe_rule.return_value["ScheduleExpression"] = "rate(5 minutes)"
        with self.assertRaisesRegex(preflight.PreflightError, "cadence"):
            preflight._inspect_schedule(
                SimpleNamespace(events=events),
                name="watcher",
                function=function,
                failure_queue_arn="arn:failure",
            )

    def test_worker_mapping_rejects_scaling_drift(self):
        function = {"name": "worker", "reserved_concurrency": 1}
        mapping = {
            "UUID": "mapping-1",
            "State": "Disabled",
            "BatchSize": 10,
            "MaximumBatchingWindowInSeconds": 0,
            "ScalingConfig": {"MaximumConcurrency": 1},
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
        }
        lambda_client = Mock()
        lambda_client.list_event_source_mappings.return_value = {"EventSourceMappings": [mapping]}
        result = preflight._inspect_worker_mapping(
            SimpleNamespace(lambda_client=lambda_client), function=function, queue_arn="arn:queue"
        )
        self.assertEqual(result["maximum_concurrency"], 1)
        mapping["ScalingConfig"] = {"MaximumConcurrency": 2}
        with self.assertRaisesRegex(preflight.PreflightError, "scaling"):
            preflight._inspect_worker_mapping(
                SimpleNamespace(lambda_client=lambda_client), function=function, queue_arn="arn:queue"
            )

    def test_credential_preview_reads_metadata_only(self):
        secrets = Mock(spec=["describe_secret"])
        secrets.describe_secret.return_value = {"Name": "credential-id", "ARN": "arn:secret"}
        result = preflight._credential_metadata(
            SimpleNamespace(secretsmanager=secrets),
            {"secret_store": "secrets_manager"},
            "credential-id",
        )
        self.assertEqual(result, {"store": "secrets_manager", "id": "credential-id", "arn": "arn:secret"})
        secrets.describe_secret.assert_called_once_with(SecretId="credential-id")

    def test_active_release_inventory_must_match_the_reviewed_deployment(self):
        config: dict[str, list[dict[str, object]]] = {"feeds": [{}, {}, {}, {}]}
        inventory = {
            "slack": {
                "default_route_id": "shared-alerts",
                "routes": {"shared-alerts": {"destination_key": "destination"}},
            }
        }
        release = SimpleNamespace(config=config, inventory=inventory)
        with patch.object(preflight.validator, "validate_semantics") as validate:
            _, route_id, route = preflight._release_route({"deployment_id": "dev"}, config, release)
        self.assertEqual(route_id, "shared-alerts")
        self.assertEqual(route["destination_key"], "destination")
        validate.assert_called_once_with({"deployment_id": "dev"}, config, inventory)

        with (
            patch.object(preflight.validator, "validate_semantics", side_effect=ValueError("route differs")),
            self.assertRaisesRegex(preflight.PreflightError, "inventory differs") as raised,
        ):
            preflight._release_route({"deployment_id": "dev"}, config, release)
        self.assertEqual(raised.exception.status, "state_refused")

    def test_delivery_index_observation_is_bounded_and_accepts_lag(self):
        observations = [(), ((1, "candidate-1"),)]
        store = Mock()
        store.query_due.side_effect = observations
        pauses: list[int] = []
        result = preflight._await_delivery_ids(
            store,
            "pending_queue",
            due_before=10,
            expected=1,
            attempts=2,
            pause=pauses.append,
        )
        self.assertEqual(result, ("candidate-1",))
        self.assertEqual(pauses, [1])

    def test_queue_preview_requires_the_deployed_fifo_settings_and_zero_reported_depth(self):
        sqs = Mock()
        sqs.get_queue_url.return_value = {"QueueUrl": "https://sqs.example/delivery.fifo"}
        sqs.get_queue_attributes.return_value = {
            "Attributes": {
                "QueueArn": "arn:queue",
                "FifoQueue": "true",
                "ContentBasedDeduplication": "false",
                "VisibilityTimeout": "1800",
                "ApproximateNumberOfMessages": "0",
                "ApproximateNumberOfMessagesNotVisible": "0",
            }
        }
        result = preflight._queue_state(SimpleNamespace(sqs=sqs), "delivery.fifo", "arn:queue")
        self.assertEqual(result["visible"], 0)
        sqs.get_queue_attributes.return_value["Attributes"]["ApproximateNumberOfMessages"] = "1"
        with self.assertRaisesRegex(preflight.PreflightError, "must be empty"):
            preflight._queue_state(SimpleNamespace(sqs=sqs), "delivery.fifo", "arn:queue")

    def test_plan_digest_refuses_changed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            saved = plan()
            digest = preflight.write_preview(path, saved)
            self.assertEqual(preflight.load_plan(path, digest), saved)
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaisesRegex(preflight.PreflightError, "digest") as raised:
                preflight.load_plan(path, digest)
        self.assertEqual(raised.exception.status, "stale_plan")

    def test_candidate_cap_and_unexpected_errors_exit_nonzero_without_provider_detail(self):
        saved_plan = plan()
        stdout = io.StringIO()
        with (
            patch.object(preflight, "load_plan", return_value=saved_plan),
            patch.object(preflight, "apply_plan", return_value={"status": "candidate_cap_exceeded"}),
            redirect_stdout(stdout),
        ):
            exit_code = preflight.main(
                ["apply", "--plan", "plan.json", "--expected-plan-sha256", "a" * 64],
                clients=SimpleNamespace(),
            )
        self.assertEqual(exit_code, preflight.EXIT_REFUSED)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "candidate_cap_exceeded")

        stderr = io.StringIO()
        with (
            patch.object(preflight, "load_plan", return_value=saved_plan),
            patch.object(preflight, "apply_plan", side_effect=RuntimeError("secret provider response")),
            redirect_stderr(stderr),
        ):
            exit_code = preflight.main(
                ["apply", "--plan", "plan.json", "--expected-plan-sha256", "a" * 64],
                clients=SimpleNamespace(),
            )
        self.assertEqual(exit_code, preflight.EXIT_AMBIGUOUS)
        self.assertEqual(json.loads(stderr.getvalue())["status"], "provider_error")
        self.assertNotIn("secret provider response", stderr.getvalue())

        stderr = io.StringIO()
        with (
            patch.object(preflight, "load_plan", return_value=saved_plan),
            patch.object(
                preflight,
                "apply_plan",
                side_effect=preflight.PreflightError("watcher_unknown", "bounded watcher outcome is unknown"),
            ),
            redirect_stderr(stderr),
        ):
            exit_code = preflight.main(
                ["apply", "--plan", "plan.json", "--expected-plan-sha256", "a" * 64],
                clients=SimpleNamespace(),
            )
        self.assertEqual(exit_code, preflight.EXIT_AMBIGUOUS)
        self.assertEqual(json.loads(stderr.getvalue())["status"], "watcher_unknown")


if __name__ == "__main__":
    unittest.main()
