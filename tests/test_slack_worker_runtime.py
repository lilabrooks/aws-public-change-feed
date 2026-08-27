"""FIFO batch, metric, and composition contracts for the Slack worker Lambda."""

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import boto3
from botocore.exceptions import ClientError
from moto import mock_aws

from aws_public_change_feed.dispatch import serialize_request
from aws_public_change_feed.slack_worker_runtime import (
    EmbeddedWorkerMetrics,
    S3ArtifactCatalog,
    SlackWorkerRuntime,
    process_fifo_batch,
)
from aws_public_change_feed.worker import QueueDelivery, WorkerResult

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
MAX_REQUEST_BYTES = 245_760


class FakeContext:
    def __init__(self, remaining):
        self.remaining = iter(remaining)

    def get_remaining_time_in_millis(self):
        return next(self.remaining)


class RecordingProcessor:
    def __init__(self, results=None, error_at=None):
        self.results = list(results or [])
        self.error_at = error_at
        self.candidates = []

    def __call__(self, candidate, queue_delivery, metrics):
        self.candidates.append(candidate)
        if self.error_at == len(self.candidates):
            raise RuntimeError("worker fault whose detail must not enter the response")
        if self.results:
            return self.results.pop(0)
        return WorkerResult(handled=True, state="posted")


def request():
    return json.loads((ROOT / "examples/delivery-request.json").read_text(encoding="utf-8"))


def record(message_id, document=None):
    delivery_request = request() if document is None else document
    return {
        "messageId": message_id,
        "body": serialize_request(delivery_request),
        "attributes": {
            "MessageGroupId": delivery_request["destination_key"],
            "MessageDeduplicationId": f"dispatch-{message_id}",
        },
    }


def metrics():
    return EmbeddedWorkerMetrics("AWSPublicChangeFeed/test", clock=lambda: NOW, emit=lambda line: None)


class FifoBatchTests(unittest.TestCase):
    def test_every_handled_record_is_acknowledged(self):
        processor = RecordingProcessor()
        event = {"Records": [record("one"), record("two"), record("three")]}

        response = process_fifo_batch(
            event,
            FakeContext([100_000, 90_000, 80_000]),
            processor,
            max_delivery_request_bytes=MAX_REQUEST_BYTES,
            safety_reserve_milliseconds=30_000,
            metrics=metrics(),
        )

        self.assertEqual(response, {"batchItemFailures": []})
        self.assertEqual(processor.candidates, [request()["candidate"]["candidate_id"]] * 3)

    def test_first_unhandled_record_and_every_later_record_are_returned(self):
        processor = RecordingProcessor(
            [
                WorkerResult(handled=True),
                WorkerResult(
                    handled=False,
                    reason="dispatch handoff timed out",
                    reason_code="dispatch_handoff_timeout",
                ),
            ]
        )
        event = {"Records": [record("one"), record("two"), record("three")]}
        output: list[str] = []
        observed = EmbeddedWorkerMetrics(
            "AWSPublicChangeFeed/test",
            clock=lambda: NOW,
            emit=output.append,
        )

        response = process_fifo_batch(
            event,
            FakeContext([100_000, 90_000]),
            processor,
            max_delivery_request_bytes=MAX_REQUEST_BYTES,
            safety_reserve_milliseconds=30_000,
            metrics=observed,
        )
        observed.flush()

        self.assertEqual(
            response,
            {"batchItemFailures": [{"itemIdentifier": "two"}, {"itemIdentifier": "three"}]},
        )
        self.assertEqual(len(processor.candidates), 2)
        document = json.loads(output[0])
        self.assertEqual(document["UnprocessedDispatchHandoffTimeout"], 1)
        self.assertEqual(document["BatchStopped"], 1)

    def test_time_reserve_stops_before_starting_the_current_record(self):
        processor = RecordingProcessor()
        event = {"Records": [record("one"), record("two"), record("three")]}

        response = process_fifo_batch(
            event,
            FakeContext([100_000, 29_999]),
            processor,
            max_delivery_request_bytes=MAX_REQUEST_BYTES,
            safety_reserve_milliseconds=30_000,
            metrics=metrics(),
        )

        self.assertEqual(
            response,
            {"batchItemFailures": [{"itemIdentifier": "two"}, {"itemIdentifier": "three"}]},
        )
        self.assertEqual(len(processor.candidates), 1)

    def test_worker_exception_stops_fifo_without_exposing_its_detail(self):
        processor = RecordingProcessor(error_at=2)
        event = {"Records": [record("one"), record("two"), record("three")]}
        output: list[str] = []
        observed = EmbeddedWorkerMetrics(
            "AWSPublicChangeFeed/test",
            clock=lambda: NOW,
            emit=output.append,
        )

        response = process_fifo_batch(
            event,
            FakeContext([100_000, 90_000]),
            processor,
            max_delivery_request_bytes=MAX_REQUEST_BYTES,
            safety_reserve_milliseconds=30_000,
            metrics=observed,
        )
        observed.flush()

        rendered = json.dumps(response)
        self.assertEqual(
            response,
            {"batchItemFailures": [{"itemIdentifier": "two"}, {"itemIdentifier": "three"}]},
        )
        self.assertNotIn("worker fault", rendered)
        self.assertEqual(json.loads(output[0])["WorkerFault"], 1)

    def test_malformed_body_stops_fifo_at_that_record(self):
        malformed = record("two")
        malformed["body"] = '{"request_id":"first","request_id":"second"}'
        event = {"Records": [record("one"), malformed, record("three")]}
        processor = RecordingProcessor()

        response = process_fifo_batch(
            event,
            FakeContext([100_000, 90_000]),
            processor,
            max_delivery_request_bytes=MAX_REQUEST_BYTES,
            safety_reserve_milliseconds=30_000,
            metrics=metrics(),
        )

        self.assertEqual(
            response,
            {"batchItemFailures": [{"itemIdentifier": "two"}, {"itemIdentifier": "three"}]},
        )
        self.assertEqual(len(processor.candidates), 1)

    def test_missing_fifo_dispatch_attributes_stop_before_processing(self):
        for missing in ("MessageGroupId", "MessageDeduplicationId"):
            with self.subTest(missing=missing):
                malformed = record("one")
                del malformed["attributes"][missing]
                processor = RecordingProcessor()

                response = process_fifo_batch(
                    {"Records": [malformed, record("two")]},
                    FakeContext([100_000]),
                    processor,
                    max_delivery_request_bytes=MAX_REQUEST_BYTES,
                    safety_reserve_milliseconds=30_000,
                    metrics=metrics(),
                )

                self.assertEqual(
                    response,
                    {"batchItemFailures": [{"itemIdentifier": "one"}, {"itemIdentifier": "two"}]},
                )
                self.assertEqual(processor.candidates, [])

    def test_invalid_event_shape_raises_so_lambda_retries_the_batch(self):
        with self.assertRaisesRegex(ValueError, "Records array"):
            process_fifo_batch(
                {},
                FakeContext([]),
                RecordingProcessor(),
                max_delivery_request_bytes=MAX_REQUEST_BYTES,
                safety_reserve_milliseconds=30_000,
                metrics=metrics(),
            )


class EmbeddedMetricTests(unittest.TestCase):
    def test_one_bounded_document_contains_only_fixed_dimensions_and_count_names(self):
        output: list[str] = []
        observed = EmbeddedWorkerMetrics(
            "AWSPublicChangeFeed/dev",
            clock=lambda: NOW,
            emit=output.append,
        )
        observed.posted()
        observed.posted()
        observed.application_version_mismatch()
        observed.unprocessed_reason("dispatch_handoff_timeout")
        observed.unprocessed_reason("unrecognized_reason")
        observed.batch_stopped()
        observed.flush()

        self.assertEqual(len(output), 1)
        document = json.loads(output[0])
        self.assertEqual(document["Posted"], 2)
        self.assertEqual(document["ApplicationVersionMismatch"], 1)
        self.assertEqual(document["UnprocessedDispatchHandoffTimeout"], 1)
        self.assertEqual(document["UnprocessedOther"], 1)
        self.assertEqual(document["BatchStopped"], 1)
        definition = document["_aws"]["CloudWatchMetrics"][0]
        self.assertEqual(definition["Namespace"], "AWSPublicChangeFeed/dev")
        self.assertEqual(definition["Dimensions"], [[]])
        self.assertNotIn(request()["candidate"]["candidate_id"], output[0])

    def test_namespace_is_bounded_before_it_reaches_logs(self):
        for invalid in ("", "AWS/Reserved", "bad namespace", "x" * 256):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                EmbeddedWorkerMetrics(invalid)


class ArtifactAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.client = boto3.client("s3", region_name="us-east-1")
        self.bucket = "worker-artifacts"
        self.client.create_bucket(Bucket=self.bucket)
        self.catalog = S3ArtifactCatalog(self.client, self.bucket, "apcf/application-artifacts")

    def test_catalog_requires_the_digest_key_and_matching_metadata(self):
        digest = "a" * 64
        key = f"apcf/application-artifacts/{digest}.zip"
        self.client.put_object(Bucket=self.bucket, Key=key, Body=b"package", Metadata={"sha256": digest})

        self.assertTrue(self.catalog.available(f"sha256:{digest}"))
        self.assertFalse(self.catalog.available(f"sha256:{'b' * 64}"))

        self.client.put_object(Bucket=self.bucket, Key=key, Body=b"package", Metadata={"sha256": "c" * 64})
        self.assertFalse(self.catalog.available(f"sha256:{digest}"))

    def test_unclassified_provider_failure_exposes_only_a_bounded_error(self):
        class DeniedClient:
            def head_object(self, **arguments):
                raise ClientError(
                    {
                        "Error": {"Code": "AccessDenied", "Message": "provider detail"},
                        "ResponseMetadata": {"HTTPStatusCode": 403},
                    },
                    "HeadObject",
                )

        catalog = S3ArtifactCatalog(DeniedClient(), self.bucket, "artifacts")
        with self.assertRaisesRegex(RuntimeError, "did not complete") as raised:
            catalog.available(f"sha256:{'a' * 64}")
        self.assertNotIn("provider detail", str(raised.exception))

    def test_version_mismatch_reports_when_the_required_package_is_absent(self):
        output: list[str] = []
        observed = EmbeddedWorkerMetrics("AWSPublicChangeFeed/dev", clock=lambda: NOW, emit=output.append)
        runtime = SlackWorkerRuntime(
            store=cast(Any, object()),
            release_store=cast(Any, object()),
            credentials=cast(Any, object()),
            sender=cast(Any, object()),
            application_version=f"sha256:{'a' * 64}",
            max_delivery_request_bytes=MAX_REQUEST_BYTES,
            lease_duration_seconds=300,
            artifact_catalog=self.catalog,
        )
        delivery_request = request()
        delivery_request["candidate"]["release"]["application_version"] = f"sha256:{'b' * 64}"
        envelope = QueueDelivery(
            delivery_request,
            "message",
            delivery_request["destination_key"],
            "dispatch-message",
        )

        with patch(
            "aws_public_change_feed.slack_worker_runtime.process_delivery",
            return_value=WorkerResult(
                handled=False,
                state="queued",
                reason_code="application_version_mismatch",
            ),
        ):
            runtime.process(delivery_request["candidate"]["candidate_id"], envelope, observed)
        observed.flush()

        document = json.loads(output[0])
        self.assertEqual(document["ArtifactUnavailable"], 1)
        self.assertEqual(document.get("ArtifactAvailabilityCheckFailed"), None)


if __name__ == "__main__":
    unittest.main()
