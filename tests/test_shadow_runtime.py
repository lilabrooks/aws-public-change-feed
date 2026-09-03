import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.loading import (  # noqa: E402
    IncompatibleRelease,
    LoadedRelease,
    ReleaseIntegrityError,
)
from aws_public_change_feed.outbox import InMemoryOutboxStore  # noqa: E402
from aws_public_change_feed.releases import ObjectMissing  # noqa: E402
from aws_public_change_feed.shadow_runtime import ShadowRuntime  # noqa: E402
from aws_public_change_feed.state import (  # noqa: E402
    InMemoryAnnouncementStateStore,
    InMemoryFeedStateStore,
    InMemorySnapshotStore,
)
from aws_public_change_feed.watcher import FeedRunOutcome, WatcherOrchestrator, WatcherResult  # noqa: E402

DIGEST = "a" * 64
APPLICATION = f"sha256:{'b' * 64}"
FEEDS = ("aws-news-blog", "aws-whats-new")


def release() -> LoadedRelease:
    return LoadedRelease(
        release_id=DIGEST,
        config={
            "feeds": [
                {"name": "aws-whats-new", "url": "https://aws.amazon.com/new/feed/", "source_type": "rss"},
                {"name": "aws-news-blog", "url": "https://aws.amazon.com/blogs/aws/feed/", "source_type": "rss"},
            ]
        },
        inventory={},
        reference={"config": {}, "inventory": {}},
    )


class RecordingOrchestrator:
    def __init__(self, outbox: InMemoryOutboxStore, *, failed: bool = False) -> None:
        self.outbox = outbox
        self.failed = failed
        self.calls: list[tuple[LoadedRelease, str, int]] = []

    def run(self, loaded, *, invocation_id, remaining_time_ms):
        self.calls.append((loaded, invocation_id, remaining_time_ms()))
        self.outbox.put_candidate_if_absent({"candidate_id": "candidate-2", "route_id": "route-b"})
        self.outbox.put_candidate_if_absent({"candidate_id": "candidate-1", "route_id": "route-a"})
        return WatcherResult(
            outcomes=(
                FeedRunOutcome(
                    feed_name="aws-news-blog",
                    status="failed" if self.failed else "fetched",
                    error_class="status" if self.failed else None,
                    item_count=3,
                    candidate_ids=("candidate-1",),
                ),
                FeedRunOutcome(
                    feed_name="aws-whats-new",
                    status="fetched",
                    item_count=5,
                    candidate_ids=("candidate-2",),
                ),
            ),
            candidate_ids=("candidate-2", "candidate-1"),
            advanced_feeds=FEEDS,
            created_delivery_ids=("candidate-1", "candidate-2"),
        )


class ShadowRuntimeTests(unittest.TestCase):
    def runtime(self, *, failed=False):
        outbox = InMemoryOutboxStore()
        orchestrator = RecordingOrchestrator(outbox, failed=failed)
        return (
            ShadowRuntime(
                release_store=Mock(),
                pointer_key="runtime/active-versions.json",
                application_version=APPLICATION,
                orchestrator=orchestrator,
                outbox=outbox,
            ),
            orchestrator,
        )

    def invoke(self, runtime, **changes):
        arguments = {
            "invocation_id": "request-1",
            "remaining_time_ms": lambda: 300_000,
            "expected_release_id": DIGEST,
            "expected_application_version": APPLICATION,
            "expected_feed_names": FEEDS,
        }
        arguments.update(changes)
        with patch("aws_public_change_feed.shadow_runtime.load_active_release", return_value=release()):
            return runtime.run(**arguments)

    def test_exact_inputs_evaluate_into_bounded_no_write_evidence(self):
        runtime, orchestrator = self.runtime()

        result = self.invoke(runtime)

        self.assertEqual(result["classification"], "passed")
        self.assertEqual(result["feed_count"], 2)
        self.assertEqual(result["normalized_item_count"], 8)
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["route_ids"], ["route-a", "route-b"])
        self.assertEqual(result["invocation_id"], "request-1")
        self.assertEqual(len(result["candidate_ids_sha256"]), 64)
        self.assertEqual(orchestrator.calls[0][1:], ("request-1", 300_000))
        self.assertNotIn("candidate_ids", result)

    def test_each_expected_identity_is_enforced_before_feed_work(self):
        mutations = (
            {"expected_release_id": "c" * 64},
            {"expected_application_version": f"sha256:{'c' * 64}"},
            {"expected_feed_names": ("aws-news-blog",)},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                runtime, orchestrator = self.runtime()
                with self.assertRaisesRegex(RuntimeError, "mismatch"):
                    self.invoke(runtime, **mutation)
                self.assertEqual(orchestrator.calls, [])

                fresh, successful = self.runtime()
                self.assertEqual(self.invoke(fresh)["classification"], "passed")
                self.assertEqual(len(successful.calls), 1)

    def test_a_feed_failure_is_retained_as_failed_evidence(self):
        runtime, _ = self.runtime(failed=True)
        result = self.invoke(runtime)
        self.assertEqual(result["classification"], "failed")
        self.assertEqual(result["outcomes"][0]["error_class"], "status")

    def test_an_incomplete_shadow_run_is_not_reported_as_evidence(self):
        runtime, orchestrator = self.runtime()
        original = orchestrator.run

        def incomplete(*args, **kwargs):
            result = original(*args, **kwargs)
            return WatcherResult(
                outcomes=result.outcomes,
                candidate_ids=result.candidate_ids,
                advanced_feeds=result.advanced_feeds,
                incomplete=True,
            )

        orchestrator.run = incomplete
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.invoke(runtime)

    def test_missing_outcome_feed_is_refused_instead_of_passing(self):
        runtime, orchestrator = self.runtime()
        original = orchestrator.run

        def missing_outcome(*args, **kwargs):
            result = original(*args, **kwargs)
            return WatcherResult(
                outcomes=result.outcomes[:1],
                candidate_ids=result.candidate_ids,
                advanced_feeds=result.advanced_feeds,
            )

        orchestrator.run = missing_outcome
        with self.assertRaisesRegex(RuntimeError, "outcome_feed_set_mismatch"):
            self.invoke(runtime)


class HandlerBoundaryTests(unittest.TestCase):
    def event(self):
        return {
            "operation": "shadow_evaluate",
            "expected_release_id": DIGEST,
            "expected_application_version": APPLICATION,
            "expected_feed_names": list(FEEDS),
        }

    def test_unknown_event_fields_are_rejected_before_environment_or_aws(self):
        from aws_public_change_feed import shadow_runtime

        event = {**self.event(), "unexpected": True}
        with (
            patch.object(shadow_runtime, "_configuration_from_environment") as configuration,
            self.assertRaisesRegex(ValueError, "invalid shadow evaluation event"),
        ):
            shadow_runtime.lambda_handler(event, object())
        configuration.assert_not_called()

    def test_handler_builds_a_fresh_ephemeral_runtime_for_every_invocation(self):
        from aws_public_change_feed import shadow_runtime

        configuration = SimpleNamespace()
        first = Mock()
        second = Mock()
        first.run.return_value = {"classification": "passed", "invocation_id": "request-1"}
        second.run.return_value = {"classification": "passed", "invocation_id": "request-1"}
        context = SimpleNamespace(aws_request_id="request-1", get_remaining_time_in_millis=lambda: 300_000)
        with (
            patch.object(shadow_runtime, "_configuration_from_environment", return_value=configuration),
            patch.object(shadow_runtime, "_build_runtime", side_effect=[first, second]) as build,
        ):
            shadow_runtime.lambda_handler(self.event(), context)
            shadow_runtime.lambda_handler(self.event(), context)
        self.assertEqual(build.call_count, 2)

    def test_handler_preserves_bounded_refusal_codes_and_hides_unexpected_details(self):
        from aws_public_change_feed import shadow_runtime

        context = SimpleNamespace(aws_request_id="request-1", get_remaining_time_in_millis=lambda: 300_000)
        cases = (
            (shadow_runtime._ShadowRefusal("expected_release_mismatch"), "expected_release_mismatch"),
            (IncompatibleRelease("private schema detail"), "release_incompatible"),
            (ReleaseIntegrityError("private hash detail"), "release_integrity_failure"),
            (ObjectMissing("private object key"), "release_missing"),
            (RuntimeError("secret-value"), "shadow evaluation failed"),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                runtime = Mock()
                runtime.run.side_effect = error
                with (
                    patch.object(shadow_runtime, "_configuration_from_environment", return_value=SimpleNamespace()),
                    patch.object(shadow_runtime, "_build_runtime", return_value=runtime),
                    self.assertRaisesRegex(RuntimeError, expected) as raised,
                ):
                    shadow_runtime.lambda_handler(self.event(), context)
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn("secret-value", str(raised.exception))

    def test_each_identity_inversion_keeps_its_code_through_the_handler(self):
        from aws_public_change_feed import shadow_runtime

        context = SimpleNamespace(aws_request_id="request-1", get_remaining_time_in_millis=lambda: 300_000)
        cases = (
            ("expected_release_id", "c" * 64, "expected_release_mismatch"),
            ("expected_application_version", f"sha256:{'c' * 64}", "expected_application_mismatch"),
            ("expected_feed_names", ["aws-news-blog"], "expected_feed_set_mismatch"),
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                outbox = InMemoryOutboxStore()
                orchestrator = RecordingOrchestrator(outbox)
                runtime = ShadowRuntime(
                    release_store=Mock(),
                    pointer_key="runtime/active-versions.json",
                    application_version=APPLICATION,
                    orchestrator=orchestrator,
                    outbox=outbox,
                )
                event = {**self.event(), field: value}
                with (
                    patch.object(shadow_runtime, "_configuration_from_environment", return_value=SimpleNamespace()),
                    patch.object(shadow_runtime, "_build_runtime", return_value=runtime),
                    patch.object(shadow_runtime, "load_active_release", return_value=release()),
                    self.assertRaisesRegex(RuntimeError, code),
                ):
                    shadow_runtime.lambda_handler(event, context)
                self.assertEqual(orchestrator.calls, [])

    def test_context_uses_the_same_leading_alphanumeric_contract_as_the_watcher(self):
        from aws_public_change_feed import shadow_runtime

        context = SimpleNamespace(aws_request_id="-request", get_remaining_time_in_millis=lambda: 300_000)
        with (
            patch.object(shadow_runtime, "_configuration_from_environment", return_value=SimpleNamespace()),
            self.assertRaisesRegex(ValueError, "invalid Lambda shadow context"),
        ):
            shadow_runtime.lambda_handler(self.event(), context)

    def test_composition_constructs_no_durable_state_client(self):
        from aws_public_change_feed import shadow_runtime

        configuration = shadow_runtime._Configuration(
            config_bucket="bucket",
            pointer_key="runtime/active-versions.json",
            application_version=APPLICATION,
            approved_hosts=("aws.amazon.com",),
            connect_timeout_seconds=2,
            response_timeout_seconds=5,
            max_response_bytes=1024,
            max_items=10,
            max_item_characters=1000,
            max_concurrent_fetches=2,
            lease_seconds=360,
        )
        with patch("boto3.client", return_value=Mock()) as client:
            runtime = shadow_runtime._build_runtime(configuration)

        client.assert_called_once_with("s3")
        self.assertIsInstance(runtime.orchestrator, WatcherOrchestrator)
        assert isinstance(runtime.orchestrator, WatcherOrchestrator)
        self.assertIsInstance(runtime.orchestrator.feed_state, InMemoryFeedStateStore)
        self.assertIsInstance(runtime.orchestrator.announcement_state, InMemoryAnnouncementStateStore)
        self.assertIsInstance(runtime.orchestrator.snapshots, InMemorySnapshotStore)
        self.assertIsInstance(runtime.orchestrator.outbox, InMemoryOutboxStore)


if __name__ == "__main__":
    unittest.main()
