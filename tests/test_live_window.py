"""Cost-control refusals and lifecycle behavior without AWS mutations."""

import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call, patch

import yaml
from botocore.exceptions import ClientError, EndpointConnectionError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import live_window as live  # noqa: E402
from preflight_delivery import PreflightError  # noqa: E402


def change(address, kind, before, after, actions=None, after_unknown=None):
    return {
        "address": address,
        "type": kind,
        "change": {
            "actions": actions or ["update"],
            "before": before,
            "after": after,
            "after_unknown": after_unknown or {},
        },
    }


class LiveWindowTests(unittest.TestCase):
    def test_delivery_refusal_records_evidence_and_outcome_before_cleanup(self):
        controller = Mock()
        controller.evidence_errors = []
        controller.owner.return_value = {
            "phase": {"S": "prepared"},
            "input": {"S": json.dumps({"case": "delivery"})},
            "cleanup_at": {"S": "2999-01-01T00:00:00Z"},
        }
        controller.delivery_test.side_effect = PreflightError("refused", "actionable delivery state exists")
        with patch.object(live, "run_command"), self.assertRaises(live.Refused):
            live.Controller.remote(controller, "unpark", "op")
        controller.delivery_test.assert_called_once()
        controller.evidence.assert_any_call(
            "op", "delivery", {"status": "refused", "detail": "actionable delivery state exists"}
        )
        controller.update.assert_called_once_with(
            "op", {"outcome": "refused", "stop": True, "phase": "test_finished", "evidence_incomplete": False}
        )

    def test_main_handles_unexpected_preflight_refusal_without_private_traceback(self):
        controller = Mock()
        controller.remote.side_effect = PreflightError("refused", "private diagnostic")
        with (
            patch.object(live, "Controller", return_value=controller),
            patch.object(Path, "read_bytes", return_value=b"{}"),
            patch.dict(os.environ, {"LIVE_ACTION": "unpark", "LIVE_OPERATION": "op"}),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(live.main(["run"]), 1)
        self.assertIn("PreflightError", stderr.getvalue())
        self.assertNotIn("private diagnostic", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_empty_exported_case_uses_observation_default(self):
        with (
            patch.dict(os.environ, {"LIVE_CONFIG": "/private/operator.json", "WINDOW": "2h", "CASE": ""}),
            patch.object(live, "read_config", return_value={}),
            patch.object(live, "Controller") as controller,
            patch.object(live, "start", return_value={"execution": "owner"}) as start,
            patch.object(live, "wait_for", return_value={"status": "parked"}),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(live.main(["test"]), 0)
        start.assert_called_once_with(controller.return_value, "2h", "observation")

    def test_interrupted_test_sanitizes_best_effort_park_failures(self):
        for error in (
            None,
            ClientError({"Error": {"Code": "AccessDenied", "Message": "PRIVATE-SENTINEL"}}, "UpdateItem"),
            EndpointConnectionError(endpoint_url="https://PRIVATE-SENTINEL.invalid"),
            live.Refused("PRIVATE-SENTINEL"),
            RuntimeError("PRIVATE-SENTINEL"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch.dict(os.environ, {"LIVE_CONFIG": "/private/operator.json", "CASE": "observation"}),
                patch.object(live, "read_config", return_value={}),
                patch.object(live, "Controller") as controller,
                patch.object(live, "start", side_effect=KeyboardInterrupt),
                patch.object(live, "request_park", side_effect=error) as park,
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                self.assertEqual(live.main(["test"]), 130)
                park.assert_called_once_with(controller.return_value)
                self.assertIn("AWS cleanup owner remains responsible", stderr.getvalue())
                self.assertEqual("Early park request not confirmed" in stderr.getvalue(), error is not None)
                self.assertNotIn("PRIVATE-SENTINEL", stderr.getvalue() + stdout.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue() + stdout.getvalue())

    def test_interrupted_unpark_does_not_request_early_park(self):
        with (
            patch.dict(os.environ, {"LIVE_CONFIG": "/private/operator.json"}),
            patch.object(live, "read_config", return_value={}),
            patch.object(live, "Controller"),
            patch.object(live, "start", side_effect=KeyboardInterrupt),
            patch.object(live, "request_park") as park,
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(live.main(["unpark"]), 130)
        park.assert_not_called()

    def test_owned_operator_config_rejects_unknown_missing_and_malformed_fields(self):
        valid = {
            "account": live.ACCOUNT,
            "region": live.REGION,
            "bucket": f"apcf-dev-live-control-{live.ACCOUNT}",
            "table": "apcf-dev-live-control",
            "project": "apcf-dev-live-control",
            "state_machine": f"arn:aws:states:{live.REGION}:{live.ACCOUNT}:stateMachine:apcf-dev-live-control",
            "tfvars": "/private/operator/central.tfvars.json",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operator.json"
            path.write_text(json.dumps(valid))
            self.assertEqual(live.read_config(path), valid)
            mutations = [{**valid, "unknown": "value"}, {**valid, "tfvars": None}]
            mutations.extend({key: value for key, value in valid.items() if key != missing} for missing in valid)
            mutations.extend({**valid, key: "wrong"} for key in valid if key != "tfvars")
            for mutated in mutations:
                path.write_text(json.dumps(mutated))
                with self.subTest(mutated=mutated), self.assertRaises(live.Refused):
                    live.read_config(path)

    def test_alarm_timestamp_evidence_is_serializable(self):
        self.assertEqual(
            json.loads(live.encode({"StateUpdatedTimestamp": datetime(2026, 9, 7, tzinfo=UTC)})),
            {"StateUpdatedTimestamp": "2026-09-07T00:00:00Z"},
        )

    def test_waiter_cannot_follow_another_session(self):
        controller = Mock()
        controller.ledger.return_value = {"execution": {"S": "new-owner"}}
        with self.assertRaisesRegex(live.Refused, "original execution old-owner"):
            live.wait_for(controller, terminal=True, expected_execution="old-owner")

    def test_delivery_request_cannot_join_an_observation(self):
        controller = Mock()
        controller.ledger.return_value = {"phase": {"S": "live"}, "input": {"S": json.dumps({"case": "observation"})}}
        with self.assertRaisesRegex(live.Refused, "another test case"):
            live.start(controller, "2h", "delivery")
        controller.verify.assert_not_called()

    def test_auxiliary_failures_still_attempt_stopping_and_monitoring_removal(self):
        for fault in ("inventory", "drain", "upload"):
            with self.subTest(fault=fault):
                controller = Mock()
                controller.plan.return_value = ({}, Path("plan"))
                controller.evidence_errors = []
                controller.owner.return_value = {"phase": {"S": "live"}}
                controller.ledger.return_value = {
                    "deadline": {"S": "2999-01-01T00:00:00Z"},
                    "outcome": {"S": "observation_in_progress"},
                }
                snap = {
                    "mapping": "Enabled",
                    "rules": {"dispatcher": "ENABLED", "reconciler": "ENABLED"},
                    "delivery_states": {},
                    "work_inventory_complete": True,
                }
                controller.snapshot.return_value = snap
                controller.verify.return_value = snap
                if fault == "inventory":
                    controller.snapshot.side_effect = [
                        EndpointConnectionError(endpoint_url="https://invalid.test"),
                        snap,
                    ]
                if fault == "drain":
                    controller.apply.side_effect = [live.Refused("drain verification failed"), None, None]
                if fault == "upload":

                    def fail_upload(*args, controller=controller):
                        controller.evidence_errors.append("upload")

                    controller.evidence.side_effect = fail_upload
                with (
                    patch.object(live, "run_command", return_value=b""),
                    patch.object(live.time, "sleep"),
                    self.assertRaisesRegex(live.Refused, "controls parked"),
                ):
                    live.Controller.remote(controller, "park", "op")
                modes = [entry.args[1] for entry in controller.apply.call_args_list]
                self.assertEqual(modes[-2:], ["stopping", "parked"])
                self.assertTrue(controller.update.call_args.args[1]["evidence_incomplete"])

    def test_incomplete_activation_evidence_is_preserved_by_parking(self):
        controller = Mock()
        controller.evidence_errors = []
        controller.plan.return_value = ({}, Path("plan"))
        controller.owner.return_value = {"phase": {"S": "live"}}
        controller.ledger.return_value = {
            "deadline": {"S": "2999-01-01T00:00:00Z"},
            "outcome": {"S": "posted"},
            "evidence_incomplete": {"BOOL": True},
        }
        controller.snapshot.return_value = {
            "mapping": "Disabled",
            "delivery_states": {},
            "work_inventory_complete": True,
        }
        controller.verify.return_value = controller.snapshot.return_value
        with (
            patch.object(live, "run_command", return_value=b""),
            patch.object(live.time, "sleep"),
            self.assertRaises(live.Refused),
        ):
            live.Controller.remote(controller, "park", "op")
        self.assertEqual(controller.update.call_args.args[1]["outcome"], "posted")
        self.assertTrue(controller.update.call_args.args[1]["evidence_incomplete"])

    def test_timeout_gracefully_interrupts_owned_subprocess(self):
        process = Mock()
        process.communicate.side_effect = [subprocess.TimeoutExpired("terraform", 1), (b"", b"")]
        process.poll.return_value = None
        process.pid = 123
        manager = Mock()
        manager.__enter__ = Mock(return_value=process)
        manager.__exit__ = Mock(return_value=False)
        with (
            patch.object(live.subprocess, "Popen", return_value=manager),
            patch.object(live.os, "killpg") as killpg,
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            live.run_command(["terraform", "plan"], timeout=1)
        self.assertEqual(killpg.call_args_list, [call(123, live.signal.SIGINT)])

    def test_stop_after_planning_prevents_apply(self):
        controller = Mock()
        controller.owner.side_effect = [{}, live.Refused("stop requested")]
        controller.plan.return_value = ({}, Path("fresh.tfplan"))
        with patch.object(live, "run_command") as command, self.assertRaisesRegex(live.Refused, "stop requested"):
            live.Controller.apply(controller, "op", "live", Path("."))
        command.assert_not_called()
        self.assertEqual(controller.owner.call_count, 2)

    def test_duration_contract(self):
        for value, seconds in {"1h10m": 4200, "1h30m": 5400, "2d3h4m5s": 183845, "364d": 31449600}.items():
            self.assertEqual(live.duration(value), seconds)
        for value in ("", "0", "0s", "-1h", "1.5h", "1m2h", "1h1h", "1h; true", "15m", "1h", "365d", "1h "):
            with self.subTest(value=value), self.assertRaises(live.Refused):
                live.duration(value)

    def test_budget_rejects_missing_unknown_and_invalid_fields(self):
        self.assertEqual(live.validate_timing_budget(live.BUDGET), live.BUDGET)
        budget: dict[str, Any] = dict(live.BUDGET)
        invalid: list[Any] = [
            [],
            None,
            {**budget, "unexpected": 1},
            {**budget, "build_timeout_minutes": 1},
            {**budget, "activation_allowance_seconds": budget["build_timeout_minutes"] * 60},
        ]
        invalid.extend({key: value for key, value in live.BUDGET.items() if key != missing} for missing in live.BUDGET)
        invalid.extend({**budget, key: value} for key in budget for value in (True, 0, -1, 1.5, "1", None))
        for budget in invalid:
            with self.subTest(budget=budget), self.assertRaises(live.Refused):
                live.validate_timing_budget(budget)

    def test_duration_reserves_one_attempt_and_refuses_short_delivery_before_reads(self):
        self.assertEqual(live.CLEANUP_RESERVE, 47 * 60)
        self.assertEqual(live.STARTUP_RESERVE, 20 * 60)
        self.assertEqual(live.MIN_WINDOW, 68 * 60)
        self.assertEqual(live.MIN_DELIVERY_WINDOW, 78 * 60)
        self.assertEqual(live.PARK_ATTEMPTS_ALLOWANCE, 142 * 60 + 30)
        self.assertEqual(live.TERMINAL_FAILURE_ALLOWANCE, 143 * 60)
        self.assertEqual(live.duration(f"{live.MIN_WINDOW}s"), live.MIN_WINDOW)
        with self.assertRaises(live.Refused):
            live.duration(f"{live.MIN_WINDOW - 1}s")
        controller = Mock()
        with self.assertRaisesRegex(live.Refused, "delivery WINDOW"):
            live.start(controller, f"{live.MIN_DELIVERY_WINDOW - 1}s", "delivery")
        controller.ledger.assert_not_called()

    def test_readiness_wait_uses_startup_budget(self):
        controller = Mock()
        controller.ledger.return_value = {"execution": {"S": "owner"}, "phase": {"S": "prepared"}}
        controller.clients = {"stepfunctions": Mock()}
        controller.clients["stepfunctions"].describe_execution.return_value = {"status": "RUNNING"}
        with (
            patch.object(live.time, "monotonic", side_effect=[0, live.STARTUP_RESERVE - 1, live.STARTUP_RESERVE + 1]),
            patch.object(live.time, "sleep") as sleep,
            self.assertRaisesRegex(live.Refused, "startup still pending"),
        ):
            live.wait_for(controller, terminal=False, expected_execution="owner")
        sleep.assert_called_once_with(5)

    def test_incomplete_evidence_refuses_even_if_workflow_reports_success(self):
        controller = Mock()
        controller.ledger.return_value = {
            "execution": {"S": "owner"},
            "phase": {"S": "parked"},
            "evidence_incomplete": {"BOOL": True},
        }
        controller.clients = {"stepfunctions": Mock()}
        controller.clients["stepfunctions"].describe_execution.return_value = {"status": "SUCCEEDED"}
        with self.assertRaisesRegex(live.Refused, "session SUCCEEDED"), patch.object(live, "try_closeout") as closure:
            live.wait_for(controller, terminal=True, expected_execution="owner")
        controller.verify.assert_not_called()
        closure.assert_not_called()

    def test_activation_reserves_remaining_terraform_work_and_rechecks_usable_time(self):
        now = datetime(2026, 9, 7, tzinfo=UTC)
        for case in ("manual", "observation", "delivery"):
            usable = live.BUDGET["delivery_seconds" if case == "delivery" else "observation_seconds"]
            activation = live.BUDGET["activation_allowance_seconds"]
            controller = Mock()
            item = {
                "phase": {"S": "prepared"},
                "input": {"S": json.dumps({"case": case})},
                "cleanup_at": {"S": live.stamp(now + timedelta(seconds=usable + activation))},
            }
            controller.owner.return_value = item
            controller.delivery_test.return_value = {"status": "posted"}
            controller.evidence_errors = []
            with (
                self.subTest(case=case),
                patch.object(live, "run_command"),
                patch.object(live, "utc", side_effect=[now, now + timedelta(seconds=activation)]),
            ):
                live.Controller.remote(controller, "unpark", "op")
            expected = ["prepared", "direct"] if case == "delivery" else ["prepared", "draining", "live"]
            self.assertEqual([entry.args[1] for entry in controller.apply.call_args_list], expected)
            controller.reset_mock()
            # A 1-second deficit before activation must stop before enabling.
            with (
                patch.object(live, "run_command"),
                patch.object(live, "utc", return_value=now + timedelta(seconds=1)),
                self.assertRaises(live.Refused),
            ):
                live.Controller.remote(controller, "unpark", "op")
            self.assertEqual([entry.args[1] for entry in controller.apply.call_args_list], ["prepared"])
            controller.reset_mock()
            # An apply overrun must stop before testing or declaring readiness.
            with (
                patch.object(live, "run_command"),
                patch.object(live, "utc", side_effect=[now, now + timedelta(seconds=activation + 1)]),
                self.assertRaises(live.Refused),
            ):
                live.Controller.remote(controller, "unpark", "op")
            controller.delivery_test.assert_not_called()
            controller.update.assert_not_called()

    def test_park_uses_both_budgeted_boundaries_and_maximum_drain(self):
        controller = Mock()
        controller.evidence_errors = []
        controller.owner.return_value = {"phase": {"S": "live"}}
        controller.ledger.return_value = {"deadline": {"S": "2999-01-01T00:00:00Z"}}
        controller.plan.return_value = ({}, Path("plan"))
        controller.snapshot.return_value = {
            "mapping": "Enabled",
            "rules": {"dispatcher": "ENABLED", "reconciler": "ENABLED"},
            "delivery_states": {"queued": 1},
            "work_inventory_complete": True,
        }
        controller.verify.return_value = controller.snapshot.return_value
        now = datetime(2026, 9, 7, tzinfo=UTC)
        later = now + timedelta(seconds=live.BUDGET["drain_seconds"] + 1)
        with (
            patch.object(live, "utc", side_effect=[now, now, later, later, later]),
            patch.object(live, "run_command"),
            patch.object(live.time, "sleep") as sleep,
        ):
            live.Controller.remote(controller, "park", "op")
        self.assertEqual(
            sleep.call_args_list,
            [call(live.BUDGET["watcher_wait_seconds"]), call(10), call(live.BUDGET["final_wait_seconds"])],
        )
        self.assertEqual(
            [entry.args[1] for entry in controller.apply.call_args_list], ["draining", "stopping", "parked"]
        )

    @unittest.skipUnless(shutil.which("terraform"), "Terraform not installed")
    def test_evaluated_build_and_retry_settings_match_shared_budget(self):
        main = (ROOT / "infra/live-control/main.tf").read_text()
        workflow = (ROOT / "infra/live-control/workflow.tf").read_text()
        timing = re.search(r"(?m)^  timing\s*= (.+)$", main)
        assert timing is not None
        expressions = {"timing": timing.group(1)}
        for name in ("build_timeout", "queued_timeout"):
            setting = re.search(rf"(?m)^  {name}\s*= (.+)$", main)
            assert setting is not None
            expressions[name] = setting.group(1)
        for name in ("IntervalSeconds", "BackoffRate", "MaxAttempts"):
            setting = re.search(rf"\b{name}\s*= ([^,\n}}]+)", workflow)
            assert setting is not None
            expressions[name] = setting.group(1)
        notification = re.search(r"TimeoutSeconds\s*= (local\.timing\.[a-z_]+)", workflow)
        assert notification is not None
        expressions["notification_timeout"] = notification.group(1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "infra/live-control"
            module.mkdir(parents=True)
            (root / "scripts").mkdir()
            shutil.copyfile(ROOT / "scripts/live_window_budget.json", root / "scripts/live_window_budget.json")
            (module / "main.tf").write_text(
                "locals {\n" + "\n".join(f"{k} = {v}" for k, v in expressions.items()) + "\n}"
            )
            result = subprocess.run(
                ["terraform", "console"],
                input="jsonencode({" + ",".join(f"{key}=local.{key}" for key in expressions if key != "timing") + "})",
                cwd=module,
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
        actual = json.loads(json.loads(result.stdout))
        self.assertEqual(
            actual,
            {
                "build_timeout": live.BUDGET["build_timeout_minutes"],
                "queued_timeout": live.BUDGET["queued_timeout_minutes"],
                "IntervalSeconds": live.BUDGET["park_retry_interval_seconds"],
                "BackoffRate": live.BUDGET["park_retry_backoff_rate"],
                "MaxAttempts": live.BUDGET["park_retry_attempts"],
                "notification_timeout": live.BUDGET["cleanup_notification_seconds"],
            },
        )
        central = (ROOT / "infra/central/locals.tf").read_text()
        timeouts = {name: int(value) for name, value in re.findall(r"(\w+)_timeout_seconds\s*= (\d+)", central)}
        self.assertGreaterEqual(live.BUDGET["watcher_wait_seconds"], timeouts["watcher"])
        self.assertGreaterEqual(live.BUDGET["final_wait_seconds"], max(timeouts.values()))

    def test_bundle_keeps_shared_timing_file(self):
        baseline = {"live_mode": "parked", "operational_sns_subscription_endpoints": {"test": "private"}}
        for name in ("watcher", "dispatcher", "worker", "reconciler"):
            baseline[f"{name}_artifact_sha256"] = "digest"
            baseline[f"{name}_artifact_version_id"] = "version"
        stream = io.BytesIO()
        timing_path = "scripts/live_window_budget.json"
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(timing_path, (ROOT / timing_path).read_bytes())
        read_bytes = Path.read_bytes
        with (
            patch.object(live, "run_command", side_effect=[b"", b"revision", stream.getvalue()]) as command,
            patch.object(
                Path,
                "read_bytes",
                autospec=True,
                side_effect=lambda path: (
                    json.dumps(baseline).encode() if str(path) == "/private/tfvars.json" else read_bytes(path)
                ),
            ),
        ):
            bundled, _ = live.bundle({"tfvars": "/private/tfvars.json"})
            archive_command = command.call_args.args[0]
            baseline["unowned_sha256"] = "still rejected"
            command.side_effect = [b"", b"revision"]
            with self.assertRaisesRegex(live.Refused, "unknown fields"):
                live.bundle({"tfvars": "/private/tfvars.json"})
        self.assertIn("scripts", archive_command)
        with zipfile.ZipFile(io.BytesIO(bundled)) as archive:
            self.assertEqual(live.validate_timing_budget(json.loads(archive.read(timing_path))), live.BUDGET)

    def test_replication_refresh_permission_is_exact_bucket_only(self):
        source = (ROOT / "infra/live-control/iam.tf").read_text()
        found = re.search(r'Sid\s*= "RefreshConfigReplication".*?\n      \}', source, re.S)
        assert found is not None
        statement = found.group()
        self.assertRegex(statement, r'Action\s*= \["s3:GetReplicationConfiguration"\]')
        self.assertIn('Resource = ["arn:aws:s3:::apcf-config-dev"]', statement)
        self.assertNotIn('"s3:Get*"', source)

    def test_mapping_uuid_is_bound_to_exact_update_permission(self):
        source = (ROOT / "infra/live-control/iam.tf").read_text()
        mapping_arns = re.findall(
            r'arn:aws:lambda:us-east-1:\$\{local.account\}:event-source-mapping:([^"\n]+)', source
        )
        self.assertEqual(mapping_arns, [live.MAPPING])

    def test_controller_reads_worker_concurrency_from_canonical_deployment(self):
        path = ROOT / "infra/central/deployment.yaml"
        deployment = yaml.safe_load(path.read_bytes())
        worker = deployment["slack"]["rate_control"]["worker_reserved_concurrency"]
        self.assertEqual(live.worker_concurrency(), worker)
        deployment["slack"]["rate_control"]["worker_reserved_concurrency"] = worker + 1
        read_bytes = Path.read_bytes
        with patch.object(
            Path,
            "read_bytes",
            autospec=True,
            side_effect=lambda target: yaml.safe_dump(deployment).encode() if target == path else read_bytes(target),
        ):
            triggers, concurrency = live.expected_controls("live")
            self.assertEqual(concurrency["worker"], worker + 1)
            controller = Mock()
            controller.snapshot.return_value = {
                "concurrency": concurrency,
                "rules": {name: "ENABLED" for name in ("watcher", "dispatcher", "reconciler")},
                "mapping": "Enabled",
                "mapping_metrics_config": None,
                "provisioned": dict.fromkeys(live.FUNCTIONS, False),
                "provisioned_pollers": False,
                "alarms": [{}] * 28,
                "dashboard": True,
            }
            self.assertTrue(all(triggers.values()))
            live.Controller.verify(controller, "live")
            function = change(
                "aws_lambda_function.slack_worker[0]",
                "aws_lambda_function",
                {"reserved_concurrent_executions": 0},
                {"reserved_concurrent_executions": worker + 1},
            )
            live.validate_plan({"resource_changes": [function]}, "live")

    def test_mapping_metrics_readback_accepts_only_absent_or_empty_in_every_phase(self):
        for mode in ("parked", "prepared", "draining", "live", "direct", "stopping", "shadow"):
            triggers, concurrency = live.expected_controls(mode)
            controller = object.__new__(live.Controller)
            controller.clients = {name: Mock() for name in ("lambda", "events", "cloudwatch")}
            lc, events, cw = (controller.clients[name] for name in ("lambda", "events", "cloudwatch"))
            events.describe_rule.side_effect = lambda Name, triggers=triggers: {
                "State": "ENABLED"
                if triggers[next(name for name, function in live.FUNCTIONS.items() if function == Name)]
                else "DISABLED"
            }
            lc.get_function_concurrency.side_effect = lambda FunctionName, concurrency=concurrency: {
                "ReservedConcurrentExecutions": concurrency[
                    next(name for name, function in live.FUNCTIONS.items() if function == FunctionName)
                ]
            }
            lc.list_provisioned_concurrency_configs.return_value = {}
            count = 0 if mode == "parked" else 21 + (7 if mode == "live" else 3 if mode == "draining" else 0)
            cw.get_paginator.return_value.paginate.return_value = [{"MetricAlarms": [{}] * count}]
            if mode == "parked":
                cw.get_dashboard.side_effect = ClientError({"Error": {"Code": "ResourceNotFound"}}, "GetDashboard")
            mapping = {
                "FunctionArn": f"arn:aws:lambda:{live.REGION}:{live.ACCOUNT}:function:{live.FUNCTIONS['worker']}",
                "EventSourceArn": f"arn:aws:sqs:{live.REGION}:{live.ACCOUNT}:apcf-delivery-dev.fifo",
                "State": "Enabled" if triggers["worker"] else "Disabled",
            }
            # Read through the actual producer and verifier; no mocked snapshot.
            config: dict[str, Any]
            for config in ({}, {"MetricsConfig": {}}, {"MetricsConfig": {"Metrics": []}}):
                with self.subTest(mode=mode, config=config):
                    lc.get_event_source_mapping.return_value = {**mapping, **config}
                    snap = controller.verify(mode)
                    self.assertEqual(snap["mapping_metrics_config"], config.get("MetricsConfig"))
            metrics: Any
            for metrics in (
                {"Metrics": ["EventCount"]},
                {"Metrics": ["UnexpectedMetric"]},
                {"Metrics": [], "UnexpectedField": []},
                {"Metrics": None},
                {"Metrics": ""},
                {"Metrics": {}},
                [],
                False,
                "",
            ):
                with self.subTest(mode=mode, metrics=metrics), self.assertRaisesRegex(live.Refused, "metrics"):
                    lc.get_event_source_mapping.return_value = {**mapping, "MetricsConfig": metrics}
                    controller.verify(mode)

    def test_mapping_toggle_keeps_empty_metrics_and_refuses_metrics_changes(self):
        empty: list[dict[str, Any]]
        forbidden: list[dict[str, Any]]
        for empty in ([], [{"metrics": []}]):
            for mode, enabled in (("draining", True), ("stopping", False)):
                before = {"enabled": not enabled, "uuid": live.MAPPING, "metrics_config": empty}
                after = {**before, "enabled": enabled}
                row = change(
                    "aws_lambda_event_source_mapping.slack_worker[0]", "aws_lambda_event_source_mapping", before, after
                )
                with self.subTest(empty=empty, mode=mode):
                    live.validate_plan({"resource_changes": [row]}, mode)
                for forbidden in ([{"metrics": ["EventCount"]}], [] if empty else [{"metrics": []}]):
                    with self.subTest(forbidden=forbidden), self.assertRaises(live.Refused):
                        row["change"]["after"] = {**after, "metrics_config": forbidden}
                        live.validate_plan({"resource_changes": [row]}, mode)

    def mapping_metrics_postconditions(self, samples):
        source = (ROOT / "infra/central/lambda.tf").read_text()
        block = source.split('resource "aws_lambda_event_source_mapping" "slack_worker" {', 1)[1].split(
            '\nresource "', 1
        )[0]
        ignores = re.findall(r"ignore_changes\s*=\s*\[([^\]]*)\]", block)
        self.assertEqual(ignores, ["metrics_config"])
        self.assertIn("postcondition {", block)
        conditions = re.findall(r"(?m)^\s*condition\s*=\s*(.+)$", block)
        self.assertEqual(len(conditions), 1)
        condition = conditions[0].replace("self.metrics_config", "config").replace("var.live_mode", "mode")
        modes = [None, "parked", "prepared", "draining", "live", "direct", "stopping", "shadow"]
        expression = (
            "jsonencode([for mode in "
            + json.dumps(modes)
            + " : [for config in "
            + json.dumps(samples)
            + " : "
            + condition
            + "]])"
        )
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                ["terraform", "console"],
                input=expression,
                cwd=directory,
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
        return dict(zip(modes, json.loads(json.loads(result.stdout)), strict=True))

    @unittest.skipUnless(shutil.which("terraform"), "Terraform not installed")
    def test_mapping_metrics_postcondition_evaluates_empty_and_enabled_representations(self):
        samples = [[], [{"metrics": []}], [{"metrics": ["EventCount"]}], [{"metrics": ["UnexpectedMetric"]}]]
        for mode, results in self.mapping_metrics_postconditions(samples).items():
            with self.subTest(mode=mode):
                self.assertEqual(
                    results, [True, True, True, True] if mode in {"stopping", "parked"} else [True, True, False, False]
                )

    @unittest.skipUnless(shutil.which("terraform"), "Terraform not installed")
    def test_metrics_drift_fences_and_removes_monitoring_before_final_refusal(self):
        # Evaluate the real HCL guard, then model only the provider boundary.
        # The actual remote/plan/apply/verify methods must still reach shutdown.
        metrics = [{"metrics": ["EventCount"]}]
        conditions = self.mapping_metrics_postconditions([metrics])
        controller = Mock(spec=live.Controller)
        controller.evidence_errors = []
        controller.owner.return_value = {"phase": {"S": "live"}}
        controller.ledger.return_value = {"phase": {"S": "parking"}}
        controller.plan.side_effect = lambda mode, work: live.Controller.plan(controller, mode, work)
        controller.apply.side_effect = lambda op, mode, work: live.Controller.apply(controller, op, mode, work)
        controller.verify.side_effect = lambda mode: live.Controller.verify(controller, mode)
        applied_mode = "live"
        planned_mode = "live"
        applied_modes = []

        def snapshot(**kwargs):
            triggers, concurrency = live.expected_controls(applied_mode)
            return {
                "concurrency": concurrency,
                "rules": {
                    name: "ENABLED" if triggers[name] else "DISABLED"
                    for name in ("watcher", "dispatcher", "reconciler")
                },
                "mapping": "Enabled" if triggers["worker"] else "Disabled",
                "mapping_metrics_config": {"Metrics": ["EventCount"]},
                "provisioned": dict.fromkeys(live.FUNCTIONS, False),
                "provisioned_pollers": False,
                "alarms": [{}] * (0 if applied_mode == "parked" else 21 if applied_mode == "stopping" else 28),
                "dashboard": applied_mode != "parked",
            }

        def provider(args):
            nonlocal planned_mode, applied_mode
            if args[2] == "init":
                return b""
            if args[2] == "plan":
                planned_mode = next(arg.split("=", 2)[2] for arg in args if arg.startswith("-var=live_mode="))
                if not conditions[planned_mode][0]:
                    raise live.Refused("terraform failed; private subprocess output was withheld")
                return b""
            if args[2] == "show":
                before = {
                    "enabled": live.expected_controls(applied_mode)[0]["worker"],
                    "uuid": live.MAPPING,
                    "metrics_config": metrics,
                }
                after = {**before, "enabled": live.expected_controls(planned_mode)[0]["worker"]}
                return live.encode(
                    {
                        "resource_changes": [
                            change(
                                "aws_lambda_event_source_mapping.slack_worker[0]",
                                "aws_lambda_event_source_mapping",
                                before,
                                after,
                            )
                        ]
                    }
                )
            self.assertEqual(args[2], "apply")
            applied_mode = planned_mode
            applied_modes.append(applied_mode)
            return b""

        controller.snapshot.side_effect = snapshot
        with patch.object(live, "run_command", side_effect=provider), patch.object(live.time, "sleep") as sleep:
            with self.assertRaisesRegex(live.Refused, "metrics"):
                live.Controller.remote(controller, "park", "op")
        self.assertEqual(applied_modes, ["stopping", "parked"])
        sleep.assert_called_once_with(live.BUDGET["final_wait_seconds"])
        controller.verify.assert_called_once_with("parked")
        self.assertEqual(snapshot()["concurrency"], dict.fromkeys(live.FUNCTIONS, 0))
        self.assertEqual(snapshot()["alarms"], [])
        self.assertFalse(snapshot()["dashboard"])
        self.assertEqual(controller.evidence_errors, ["drain"])
        controller.update.assert_called_once_with("op", {"phase": "parking", "stop": True})
        self.assertNotIn("receipt", [entry.args[1] for entry in controller.evidence.call_args_list])

    @unittest.skipUnless(shutil.which("terraform"), "Terraform not installed")
    def test_failure_queue_policy_rendering_preserves_exact_grants_and_optional_runtimes(self):
        source = (ROOT / "infra/central/sqs.tf").read_text()
        self.assertNotIn('data "aws_iam_policy_document" "runtime_failure_queue"', source)
        consumer = source.split('resource "aws_sqs_queue_policy" "runtime_failures" {', 1)[1]
        self.assertRegex(consumer, r"policy\s*= local\.runtime_failure_queue_policy\s")
        self.assertNotIn("ignore_changes", consumer)
        start = source.index("\nlocals {") + 1
        rendering = source[start : source.index('\nresource "aws_sqs_queue_policy"', start)]
        # Substitute only external resource inputs, keeping the complete actual
        # rendering expression and its conditional statement membership.
        for reference, fixture in {
            "local.watcher_runtime_enabled": "var.inputs.watcher_enabled",
            "local.dispatcher_runtime_enabled": "var.inputs.dispatcher_enabled",
            "aws_sqs_queue.runtime_failures.arn": "var.inputs.queue",
            "aws_cloudwatch_event_rule.watcher[0].arn": "var.inputs.watcher[0]",
            "aws_cloudwatch_event_rule.dispatcher[0].arn": "var.inputs.dispatcher[0]",
            "aws_cloudwatch_event_rule.reconciler.arn": "var.inputs.reconciler",
            "data.aws_caller_identity.current.account_id": "var.inputs.account",
        }.items():
            rendering = rendering.replace(reference, fixture)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tf").write_text('variable "inputs" { type = any }\n' + rendering)
            for account, region in (("111111111111", "us-east-1"), ("222222222222", "eu-west-1")):
                for watcher in (False, True):
                    for dispatcher in (False, True):
                        queue = f"arn:aws:sqs:{region}:{account}:failures-test"
                        rules = {
                            name: f"arn:aws:events:{region}:{account}:rule/test-{name}"
                            for name in ("watcher", "dispatcher", "reconciler")
                        }
                        inputs = {
                            "account": account,
                            "queue": queue,
                            "watcher_enabled": watcher,
                            "dispatcher_enabled": dispatcher,
                            "watcher": [rules["watcher"]] if watcher else [],
                            "dispatcher": [rules["dispatcher"]] if dispatcher else [],
                            "reconciler": rules["reconciler"],
                        }
                        with self.subTest(account=account, watcher=watcher, dispatcher=dispatcher):
                            result = subprocess.run(
                                ["terraform", "console", "-var=inputs=" + json.dumps(inputs)],
                                input="local.runtime_failure_queue_policy",
                                cwd=root,
                                capture_output=True,
                                text=True,
                                check=True,
                                timeout=60,
                            )
                            policy = json.loads(json.loads(result.stdout))
                            names = (["watcher"] if watcher else []) + (["dispatcher"] if dispatcher else [])
                            names.append("reconciler")
                            expected = {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Sid": f"AllowExact{name.title()}Schedule",
                                        "Effect": "Allow",
                                        "Action": "sqs:SendMessage",
                                        "Resource": queue,
                                        "Principal": {"Service": "events.amazonaws.com"},
                                        "Condition": {
                                            "ArnEquals": {"aws:SourceArn": rules[name]},
                                            "StringEquals": {"aws:SourceAccount": account},
                                        },
                                    }
                                    for name in names
                                ],
                            }
                            self.assertEqual(policy, expected)

    def test_failure_queue_policy_changes_remain_outside_toggle_allowlist(self):
        for policy, unknown in (("changed-policy", {}), (None, {"policy": True})):
            row = change(
                "aws_sqs_queue_policy.runtime_failures",
                "aws_sqs_queue_policy",
                {"policy": "retained-policy"},
                {"policy": policy},
                after_unknown=unknown,
            )
            for mode in ("parked", "prepared", "draining", "live", "stopping"):
                with self.subTest(mode=mode, unknown=unknown), self.assertRaises(live.Refused):
                    live.validate_plan({"resource_changes": [row]}, mode)

    def test_tag_changes_remain_outside_every_toggle_phase(self):
        tags = {"project": "aws-public-change-feed", "component": "runtime"}
        for mode in ("parked", "prepared", "direct", "draining", "live", "shadow", "stopping"):
            triggers, concurrency = live.expected_controls(mode)
            cases = (
                (
                    "aws_lambda_function.slack_worker[0]",
                    "aws_lambda_function",
                    "reserved_concurrent_executions",
                    concurrency["worker"],
                ),
                (
                    "aws_cloudwatch_event_rule.watcher[0]",
                    "aws_cloudwatch_event_rule",
                    "state",
                    "ENABLED" if triggers["watcher"] else "DISABLED",
                ),
                (
                    "aws_lambda_event_source_mapping.slack_worker[0]",
                    "aws_lambda_event_source_mapping",
                    "enabled",
                    triggers["worker"],
                ),
            )
            for address, kind, field, target in cases:
                before: dict[str, Any] = {field: None, "tags": tags, "tags_all": tags}
                if kind == "aws_lambda_event_source_mapping":
                    before["uuid"] = live.MAPPING
                after = {**before, field: target}
                good = change(address, kind, before, after)
                with self.subTest(mode=mode, address=address):
                    live.validate_plan({"resource_changes": [good]}, mode)
                    for attribute in ("tags", "tags_all"):
                        for replacement in ({**tags, "owner": "changed"}, {}, None):
                            bad = copy.deepcopy(good)
                            bad["change"]["after"][attribute] = replacement
                            with self.assertRaises(live.Refused):
                                live.validate_plan({"resource_changes": [bad]}, mode)
                        bad = copy.deepcopy(good)
                        bad["change"]["after_unknown"] = {attribute: {"component": True}}
                        with self.assertRaises(live.Refused):
                            live.validate_plan({"resource_changes": [bad]}, mode)

    def test_computed_unknowns_are_accepted_but_known_or_configurable_changes_are_refused(self):
        cases: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = [
            (
                "aws_lambda_function",
                "aws_lambda_function.slack_worker[0]",
                {"reserved_concurrent_executions": live.worker_concurrency()},
                {"reserved_concurrent_executions": 0},
            ),
            (
                "aws_lambda_event_source_mapping",
                "aws_lambda_event_source_mapping.slack_worker[0]",
                {"enabled": True, "uuid": live.MAPPING},
                {"enabled": False, "uuid": live.MAPPING},
            ),
        ]
        for kind, address, before, after in cases:
            for attribute in live.COMPUTED_ONLY[kind]:
                old = before.get(attribute, "old-computed-value")
                good = change(
                    address,
                    kind,
                    {**before, attribute: old},
                    {**after, attribute: None},
                    after_unknown={attribute: True},
                )
                with self.subTest(kind=kind, attribute=attribute):
                    live.validate_plan({"resource_changes": [good]}, "stopping")
                    bad = copy.deepcopy(good)
                    bad["change"]["after_unknown"] = {attribute: False}
                    with self.assertRaises(live.Refused):
                        live.validate_plan({"resource_changes": [bad]}, "stopping")
            configurable = (
                ("s3_key", "s3_object_version", "image_uri", "role")
                if kind == "aws_lambda_function"
                else ("function_name", "event_source_arn", "filter_criteria")
            )
            for attribute in configurable:
                for unknown in (True, [{"nested": True}]):
                    # Before/after both null must not hide a newly unknown input.
                    bad = change(
                        address,
                        kind,
                        {**before, attribute: None},
                        {**after, attribute: None},
                        after_unknown={attribute: unknown},
                    )
                    with self.subTest(attribute=attribute, unknown=unknown), self.assertRaises(live.Refused):
                        live.validate_plan({"resource_changes": [bad]}, "stopping")

    @unittest.skipUnless(shutil.which("terraform"), "Terraform not installed")
    def test_computed_allowlist_matches_pinned_provider_schema_offline(self):
        # The repository gate initializes providers before tests on a clean CI
        # runner; standalone unittest may explicitly skip when none are cached.
        makefile = (ROOT / "Makefile").read_text()
        prerequisites = re.search(r"(?m)^check: (.*)$", makefile)
        assert prerequisites is not None
        targets = prerequisites.group(1).split()
        self.assertLess(targets.index("terraform-check"), targets.index("test"))
        lock = (ROOT / "infra/central/.terraform.lock.hcl").read_text()
        match = re.search(r'provider "registry.terraform.io/hashicorp/aws" \{\s+version\s*= "([^"]+)"', lock)
        assert match is not None
        version = match.group(1)
        candidates = [ROOT / "infra/central/.terraform/providers"]
        if os.environ.get("TF_DATA_DIR"):
            candidates.insert(0, Path(os.environ["TF_DATA_DIR"]) / "providers")
        mirror = next(
            (
                path
                for path in candidates
                if list((path / "registry.terraform.io/hashicorp/aws" / version).glob("*/terraform-provider-aws*"))
            ),
            None,
        )
        if mirror is None:
            self.skipTest("pinned AWS provider is not cached; run terraform-check before this schema test")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tf").write_text(
                'terraform {\n required_providers {\n aws = {\n source = "hashicorp/aws"\n version = '
                + json.dumps(version)
                + "\n }\n }\n}\n"
            )
            (root / ".terraform.lock.hcl").write_text(lock)
            (root / "terraform.rc").write_text(
                "provider_installation {\n filesystem_mirror {\n path = "
                + json.dumps(str(mirror.resolve()))
                + '\n include = ["registry.terraform.io/hashicorp/aws"]\n }\n}\n'
            )
            environment = {
                key: value for key, value in os.environ.items() if key not in {"TF_DATA_DIR", "TF_PLUGIN_CACHE_DIR"}
            }
            environment["TF_CLI_CONFIG_FILE"] = str(root / "terraform.rc")
            subprocess.run(
                ["terraform", "init", "-backend=false", "-input=false", "-lockfile=readonly"],
                cwd=root,
                env=environment,
                capture_output=True,
                check=True,
                timeout=60,
            )
            result = subprocess.run(
                ["terraform", "providers", "schema", "-json"],
                cwd=root,
                env=environment,
                capture_output=True,
                check=True,
                timeout=60,
            )
        schemas = json.loads(result.stdout)["provider_schemas"]["registry.terraform.io/hashicorp/aws"][
            "resource_schemas"
        ]
        derived = {
            name: {
                key
                for key, value in schemas[name]["block"]["attributes"].items()
                if value.get("computed") and not value.get("optional") and not value.get("required")
            }
            for name in live.COMPUTED_ONLY
        }
        self.assertEqual(live.COMPUTED_ONLY, derived)

    def test_field_allowlist_binds_real_worker_addresses_and_rejects_code_changes(self):
        function = change(
            "aws_lambda_function.slack_worker[0]",
            "aws_lambda_function",
            {"reserved_concurrent_executions": 2, "s3_key": "retained.zip"},
            {"reserved_concurrent_executions": 0, "s3_key": "retained.zip"},
        )
        mapping = change(
            "aws_lambda_event_source_mapping.slack_worker[0]",
            "aws_lambda_event_source_mapping",
            {"enabled": True, "uuid": live.MAPPING},
            {"enabled": False, "uuid": live.MAPPING},
        )
        plan = {"resource_changes": [function, mapping]}
        live.validate_plan(plan, "parked")
        bad = copy.deepcopy(plan)
        bad["resource_changes"][0]["change"]["after"]["s3_key"] = "replacement.zip"
        with self.assertRaises(live.Refused):
            live.validate_plan(bad, "parked")
        bad = copy.deepcopy(plan)
        bad["resource_changes"][1]["change"]["actions"] = ["delete", "create"]
        with self.assertRaises(live.Refused):
            live.validate_plan(bad, "parked")

    def test_every_real_alarm_can_be_deleted_at_park_and_trigger_alarms_at_stop(self):
        source = (ROOT / "infra/central/alarms.tf").read_text()
        blocks = re.split(r'(?m)^resource "aws_cloudwatch_metric_alarm" "', source)[1:]
        self.assertEqual(len(blocks), 28)
        for block in blocks:
            resource = block.split('"', 1)[0]
            match = re.search(r'alarm_name\s*= "([^"]+)"', block)
            assert match is not None
            name = match.group(1).replace("${local.deployment_id}", "dev")
            deleted = change(
                f"aws_cloudwatch_metric_alarm.{resource}[0]",
                "aws_cloudwatch_metric_alarm",
                {"alarm_name": name},
                None,
                ["delete"],
            )
            live.validate_plan({"resource_changes": [deleted]}, "parked")
            if "count = local.monitoring_enabled" not in block:
                live.validate_plan({"resource_changes": [deleted]}, "stopping")
            else:
                with self.assertRaises(live.Refused):
                    live.validate_plan({"resource_changes": [deleted]}, "stopping")

    def test_data_deletion_and_output_drift_are_refused(self):
        for address, kind in (
            ("aws_s3_bucket.config", "aws_s3_bucket"),
            ("aws_dynamodb_table.delivery", "aws_dynamodb_table"),
            ("aws_cloudwatch_log_group.watcher", "aws_cloudwatch_log_group"),
        ):
            with self.subTest(address=address), self.assertRaises(live.Refused):
                live.validate_plan(
                    {"resource_changes": [change(address, kind, {"id": "retained"}, None, ["delete"])]}, "parked"
                )
        with self.assertRaises(live.Refused):
            live.validate_plan({"output_changes": {"worker_application_version": {"actions": ["update"]}}}, "parked")

    def test_owner_blocks_stale_expired_and_stopping_activation(self):
        controller = object.__new__(live.Controller)
        controller.clients = {"stepfunctions": Mock()}
        controller.clients["stepfunctions"].describe_execution.return_value = {"status": "RUNNING"}
        item = {
            "generation": {"S": "op"},
            "execution": {"S": "exec"},
            "phase": {"S": "starting"},
            "stop": {"BOOL": False},
            "cleanup_at": {"S": "2999-01-01T00:00:00Z"},
        }
        controller.ledger = Mock(return_value=item)  # type: ignore[method-assign]
        with patch.dict(os.environ, {"LIVE_EXECUTION": "exec"}):
            controller.owner("op", enabling=True)
            with self.assertRaises(live.Refused):
                controller.owner("older", enabling=True)
            for key, value in (
                ("stop", {"BOOL": True}),
                ("phase", {"S": "parking"}),
                ("cleanup_at", {"S": "2000-01-01T00:00:00Z"}),
            ):
                bad = copy.deepcopy(item)
                bad[key] = value
                controller.ledger.return_value = bad
                with self.subTest(key=key), self.assertRaises(live.Refused):
                    controller.owner("op", enabling=True)

    def test_early_park_uses_callback_without_cancelling_owner(self):
        controller = Mock()
        item = {
            "generation": {"S": "op"},
            "phase": {"S": "live"},
            "execution": {"S": "execution"},
            "callback_token": {"S": "private-token"},
        }
        controller.ledger.return_value = item
        controller.update.return_value = item
        sf = Mock()
        sf.describe_execution.return_value = {"status": "RUNNING"}
        controller.clients = {"stepfunctions": sf}
        live.request_park(controller)
        sf.send_task_success.assert_called_once_with(taskToken="private-token", output="{}")
        sf.stop_execution.assert_not_called()
        sf.start_execution.assert_not_called()

    def test_already_parked_is_read_only(self):
        controller = Mock()
        controller.ledger.return_value = {"phase": {"S": "parked"}}
        self.assertEqual(live.request_park(controller), {"status": "parked"})
        controller.verify.assert_called_once_with("parked")
        controller.update.assert_not_called()

    def test_wait_has_no_polling_history_growth_and_cleanup_catches_errors(self):
        source = (ROOT / "infra/live-control/workflow.tf").read_text()
        self.assertIn("dynamodb:updateItem.waitForTaskToken", source)
        self.assertIn("$toMillis($states.input.cleanup_at) - $millis()", source)
        self.assertRegex(source, r'ConditionExpression\s*= "generation = :generation AND #stop = :no"')
        # AWS SDK integration uses PascalCase, unlike boto3's BOOL attribute tag.
        # AWS ValidateStateMachineDefinition rejects the latter here.
        self.assertRegex(source, r'":no"\s*= \{ Bool = false \}')
        self.assertNotIn("BOOL", source)
        self.assertIn('"States.Timeout", "DynamoDb.ConditionalCheckFailedException"', source)
        self.assertNotIn('Type = "Wait"', source)
        self.assertNotIn("StopExecution", source)

    @unittest.skipUnless(shutil.which("terraform"), "Terraform not installed")
    def test_evaluated_terraform_modes_match_controller_including_legacy_null(self):
        source = (ROOT / "infra/central/locals.tf").read_text()
        managed = (ROOT / "infra/central/live_mode.tf").read_text()
        definitions = []
        names = [
            f"{name}_{suffix}"
            for name in ("watcher", "dispatcher", "worker")
            for suffix in ("trigger_requested", "trigger_enabled", "reserved_concurrency")
        ]
        names += ["reconciler_trigger_enabled", "reconciler_reserved_concurrency", "shadow_reserved_concurrency"]
        for name in names:
            match = re.search(rf"(?m)^  {name}\s*= (.*)$", source)
            assert match is not None
            expression = match.group(1)
            if expression == "(":
                expression = source[match.end() : source.index("\n  )", match.end()) + 4].strip()
                expression = "(" + expression
            definitions.append(f"{name} = {expression}")
        controls = managed[
            managed.index("locals {") + len("locals {") : managed.index("\n}\n", managed.index("locals {"))
        ]
        header = "\n".join(
            f'variable "{name}" {{ default = {value} }}'
            for name, value in {
                "live_mode": "null",
                "watcher_execution_paused": "false",
                "delivery_triggers_enabled": "false",
                "reconciler_trigger_enabled": "false",
                "watcher_trigger_enabled_override": "null",
                "dispatcher_trigger_enabled_override": "null",
                "worker_trigger_enabled_override": "null",
            }.items()
        )
        definitions += [f"{name}_runtime_enabled = true" for name in ("watcher", "dispatcher", "worker", "reconciler")]
        deployment = yaml.safe_load((ROOT / "infra/central/deployment.yaml").read_bytes())
        worker = deployment["slack"]["rate_control"]["worker_reserved_concurrency"]
        definitions.append(f"rate_control = {{ worker_reserved_concurrency = {worker} }}")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "main.tf").write_text(header + "\nlocals {\n" + controls + "\n" + "\n".join(definitions) + "\n}\n")
            for mode in ("parked", "prepared", "live", "direct", "draining", "stopping", "shadow", None):
                expression = (
                    "jsonencode({triggers={"
                    + ",".join(
                        f"{name}=local.{name}_trigger_enabled"
                        for name in ("watcher", "dispatcher", "worker", "reconciler")
                    )
                    + "},concurrency={"
                    + ",".join(f"{name}=local.{name}_reserved_concurrency" for name in live.FUNCTIONS)
                    + "}})"
                )
                args = ["terraform", "console"] + ([] if mode is None else [f"-var=live_mode={mode}"])
                result = subprocess.run(
                    args, input=expression, text=True, capture_output=True, cwd=path, check=False, timeout=60
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = json.loads(json.loads(result.stdout))
                expected = (
                    live.expected_controls(mode)
                    if mode
                    else (
                        dict.fromkeys(("watcher", "dispatcher", "worker", "reconciler"), False),
                        {"watcher": 1, "dispatcher": 1, "worker": worker, "reconciler": 1, "shadow": 1},
                    )
                )
                self.assertEqual(actual, dict(zip(("triggers", "concurrency"), expected, strict=True)), mode)


if __name__ == "__main__":
    unittest.main()
