"""The Slack HTTP transport: facts in, facts out, no delivery decisions.

Every test drives a fake connection, so nothing here contacts Slack. The fake
matches `PinnedHTTPSConnection`'s constructor exactly — hostname, address, port,
timeout, context — because the pinning claim is that the socket goes to the
validated address while the certificate is checked against the approved name,
and that claim is only observable in those five arguments.

`BytesSentTruthTableTests` is written against the table ADR-004's retry-safety
rule needs, and it is the file's centre. `bytes_sent` is the single fact the
worker is allowed to rest an automatic retry on, so each row pins one boundary:
what the adapter may claim `False` for, and what it must claim `True` for even
when a send probably did not happen.

No test asserts a credential. The webhook URL and bot token used below are
obvious placeholders, and the redaction tests search raised messages, returned
values, captured log records, and the recorded request for them.
"""

import io
import json
import logging
import ssl
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

from test_worker import (  # noqa: E402
    BOT_CHANNEL_ID,
    BOT_TOKEN_SECRET_ID,
    WorkerFixture,
    bot_mode_inventory,
)

from aws_public_change_feed.credentials import BOT_TOKEN, WEBHOOK, SlackCredential  # noqa: E402
from aws_public_change_feed.slack_transport import (  # noqa: E402
    MAX_SLACK_RESPONSE_BYTES,
    SLACK_API_HOST,
    SLACK_POST_MESSAGE_PATH,
    SlackHttpSender,
)
from aws_public_change_feed.worker import (  # noqa: E402
    DELIVERY_UNKNOWN,
    FAILED_RETRYABLE,
    FAILED_TERMINAL,
    POSTED,
    SlackDestination,
    TransportError,
    _classify,
)

APPROVED_HOSTS = ("hooks.slack.com",)

# Placeholders, not credentials. Distinctive so a substring search for them in
# messages, logs, and returned values proves absence rather than luck.
WEBHOOK_URL = "https://hooks.slack.com/services/TPLACEHOLDER/BPLACEHOLDER/NotARealWebhookSecret0000"
WEBHOOK_SECRET_SEGMENT = "NotARealWebhookSecret0000"
BOT_TOKEN_VALUE = "xoxb-PLACEHOLDER-NOT-A-REAL-TOKEN-0123456789"

WEBHOOK_DESTINATION = SlackDestination(mode=WEBHOOK, approved_webhook_hosts=APPROVED_HOSTS)
BOT_DESTINATION = SlackDestination(mode=BOT_TOKEN, channel_id=BOT_CHANNEL_ID)
WEBHOOK_CREDENTIAL = SlackCredential(WEBHOOK, WEBHOOK_URL)
BOT_CREDENTIAL = SlackCredential(BOT_TOKEN, BOT_TOKEN_VALUE)

PAYLOAD = {"text": "Potentially relevant AWS change candidate", "blocks": [], "mrkdwn": False}

# Genuinely global addresses. The documentation ranges (203.0.113.0/24,
# 198.51.100.0/24, 2001:db8::/32) are reserved, so `ipaddress.is_global` is
# false for them and `validate_addresses` refuses them — which is correct, and
# makes them useless as the "public answer" fixture.
PUBLIC_ADDRESS = "93.184.216.34"
PUBLIC_ADDRESS_ALT = "13.32.99.10"
PUBLIC_ADDRESS_V6 = "2606:4700::1111"


class FakeResponse:
    """The subset of `http.client.HTTPResponse` the adapter reads."""

    def __init__(self, status, body=b"", headers=None, read_error=None):
        self.status = status
        self.headers = dict(headers or {})
        self._body = body
        self._read_error = read_error
        self.read_calls = []

    def read(self, amount=None):
        self.read_calls.append(amount)
        if self._read_error is not None:
            raise self._read_error
        return self._body if amount is None else self._body[:amount]


class FakeConnection:
    """Stands in for `PinnedHTTPSConnection`, recording the pinning arguments."""

    def __init__(self, hostname, address, port, timeout, context):
        self.hostname = hostname
        self.address = address
        self.port = port
        self.timeout = timeout
        self.context = context
        self.connected = False
        self.closed = False
        self.requests = []
        self.connect_error = None
        self.request_error = None
        self.response = FakeResponse(200, b"ok")

    def connect(self):
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    def request(self, method, path, body=None, headers=None):
        if not self.connected:
            raise AssertionError("request() was called before connect(); the boundary would be unobservable")
        self.requests.append({"method": method, "path": path, "body": body, "headers": dict(headers or {})})
        if self.request_error is not None:
            raise self.request_error

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class Recorder:
    """A connection factory that captures every connection it hands out."""

    def __init__(self, **behaviour):
        self.behaviour = behaviour
        self.connections = []

    def __call__(self, hostname, address, port, timeout, context):
        connection = FakeConnection(hostname, address, port, timeout, context)
        for name, value in self.behaviour.items():
            setattr(connection, name, value)
        self.connections.append(connection)
        return connection

    @property
    def only(self):
        if len(self.connections) != 1:
            raise AssertionError(f"expected exactly one connection, got {len(self.connections)}")
        return self.connections[0]


def fixed_resolver(*addresses):
    def resolve(hostname, port):
        return list(addresses)

    return resolve


def failing_resolver(error):
    def resolve(hostname, port):
        raise error

    return resolve


def sender(recorder=None, resolver=None, **kwargs):
    return SlackHttpSender(
        resolver=resolver or fixed_resolver(PUBLIC_ADDRESS),
        connection_factory=recorder or Recorder(),
        ssl_context_factory=ssl.create_default_context,
        **kwargs,
    )


def post(recorder=None, *, credential=WEBHOOK_CREDENTIAL, destination=WEBHOOK_DESTINATION, resolver=None, **kwargs):
    recorder = recorder if recorder is not None else Recorder()
    client = sender(recorder, resolver, **kwargs)
    response = client.post(PAYLOAD, credential=credential, destination=destination, timeout_seconds=10.0)
    return response, recorder


class _CollectingHandler(logging.Handler):
    def __init__(self, records):
        super().__init__()
        self._records = records

    def emit(self, record):
        self._records.append(record)


class LogCapture:
    """Collects every record emitted anywhere during a block."""

    def __init__(self, case):
        self.records: list[logging.LogRecord] = []
        handler = _CollectingHandler(self.records)
        root = logging.getLogger()
        root.addHandler(handler)
        previous = root.level
        root.setLevel(logging.DEBUG)
        case.addCleanup(root.removeHandler, handler)
        case.addCleanup(root.setLevel, previous)

    @property
    def text(self):
        return "\n".join(str(record.getMessage()) for record in self.records)


class BytesSentTruthTableTests(unittest.TestCase):
    """The table ADR-004's retry-safety rule is written against.

    `bytes_sent=False` is the affirmative proof that permits an automatic retry,
    so the adapter may only claim it where no request byte can have left. Every
    row below fixes one boundary, and the two timeout rows are the pair that
    matters most: the same exception class on either side of `connect()` reports
    a different fact, because only one side can be proved.
    """

    def test_malformed_input_before_socket_creation(self):
        response, recorder = post(credential=SlackCredential(WEBHOOK, "https://evil.example/services/A/B/C"))

        self.assertIs(response.bytes_sent, False)
        self.assertIs(response.error_class, TransportError.MALFORMED_URL)
        self.assertIsNone(response.status_code)
        self.assertEqual(recorder.connections, [], "no socket may be created for an unconstructable request")

    def test_refused_address_set_before_socket_creation(self):
        response, recorder = post(resolver=fixed_resolver("127.0.0.1"))

        self.assertIs(response.bytes_sent, False)
        self.assertIs(response.error_class, TransportError.MALFORMED_URL)
        self.assertEqual(recorder.connections, [])

    def test_resolution_failure_before_socket_creation(self):
        response, recorder = post(resolver=failing_resolver(OSError("temporary failure in name resolution")))

        self.assertIs(response.bytes_sent, False)
        self.assertIs(response.error_class, TransportError.CONNECT_FAILED)
        self.assertEqual(recorder.connections, [])

    def test_connect_failure_before_request_bytes(self):
        response, recorder = post(Recorder(connect_error=ConnectionRefusedError("refused")))

        self.assertIs(response.bytes_sent, False)
        self.assertIs(response.error_class, TransportError.CONNECT_FAILED)
        self.assertEqual(recorder.only.requests, [], "no request may be written after a failed connect")

    def test_connect_timeout_before_request_bytes(self):
        """A timeout during connect is provably pre-write, so it may claim False."""

        response, recorder = post(Recorder(connect_error=TimeoutError("connect timed out")))

        self.assertIs(response.bytes_sent, False)
        self.assertIs(response.error_class, TransportError.CONNECT_FAILED)
        self.assertEqual(recorder.only.requests, [])

    def test_tls_failure_before_request_bytes(self):
        response, recorder = post(Recorder(connect_error=ssl.SSLCertVerificationError("hostname mismatch")))

        self.assertIs(response.bytes_sent, False)
        self.assertIs(response.error_class, TransportError.TLS_FAILED)
        self.assertEqual(recorder.only.requests, [])

    def test_timeout_once_a_write_may_have_begun(self):
        """The same exception class as the connect row, and the opposite fact."""

        response, recorder = post(Recorder(request_error=TimeoutError("read timed out")))

        self.assertIs(response.bytes_sent, True)
        self.assertIs(response.error_class, TransportError.TIMEOUT)
        self.assertEqual(len(recorder.only.requests), 1)

    def test_connection_loss_once_a_write_may_have_begun(self):
        for error in (ConnectionResetError("reset by peer"), BrokenPipeError("broken pipe")):
            with self.subTest(error=type(error).__name__):
                response, _ = post(Recorder(request_error=error))

                self.assertIs(response.bytes_sent, True)
                self.assertIs(response.error_class, TransportError.CONNECTION_LOST)

    def test_a_protocol_failure_once_a_write_may_have_begun(self):
        import http.client

        response, _ = post(Recorder(request_error=http.client.BadStatusLine("garbage")))

        self.assertIs(response.bytes_sent, True)
        self.assertIs(response.error_class, TransportError.CONNECTION_LOST)

    def test_a_post_handshake_tls_error_reports_a_read_failure(self):
        response, _ = post(Recorder(request_error=ssl.SSLError("decryption failed")))

        self.assertIs(response.bytes_sent, True)
        self.assertIs(response.error_class, TransportError.READ_FAILED)

    def test_a_generic_socket_error_once_a_write_may_have_begun(self):
        response, _ = post(Recorder(request_error=OSError("unexpected")))

        self.assertIs(response.bytes_sent, True)
        self.assertIs(response.error_class, TransportError.READ_FAILED)

    def test_any_received_status_reports_bytes_sent_and_the_status(self):
        for status in (200, 302, 400, 403, 404, 429, 500, 502, 503):
            with self.subTest(status=status):
                response, _ = post(Recorder(response=FakeResponse(status, b"ok")))

                self.assertIs(response.bytes_sent, True)
                self.assertEqual(response.status_code, status)
                self.assertIsNone(response.error_class)

    def test_the_adapter_never_returns_a_delivery_state(self):
        """`SlackResponse` has no field that can carry a worker decision."""

        response, _ = post()
        rendered = repr(response)

        for state in (POSTED, FAILED_RETRYABLE, FAILED_TERMINAL, DELIVERY_UNKNOWN):
            self.assertNotIn(state, rendered)


class WebhookRequestTests(unittest.TestCase):
    def test_the_request_shape_is_a_deterministic_json_post(self):
        response, recorder = post()
        request = recorder.only.requests[0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/services/TPLACEHOLDER/BPLACEHOLDER/NotARealWebhookSecret0000")
        self.assertEqual(request["headers"]["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(request["headers"]["Accept-Encoding"], "identity")
        self.assertEqual(request["headers"]["Connection"], "close")
        self.assertEqual(request["headers"]["Content-Length"], str(len(request["body"])))
        self.assertEqual(json.loads(request["body"].decode("utf-8")), PAYLOAD)

    def test_serialization_is_byte_identical_across_calls(self):
        first, first_recorder = post()
        second, second_recorder = post()

        self.assertEqual(first_recorder.only.requests[0]["body"], second_recorder.only.requests[0]["body"])
        self.assertEqual(first.status_code, second.status_code)

    def test_the_connection_targets_the_validated_address_with_the_original_name(self):
        """The pinning claim: socket to the address, TLS and Host to the name."""

        _, recorder = post()
        connection = recorder.only

        self.assertEqual(connection.address, PUBLIC_ADDRESS)
        self.assertEqual(connection.hostname, "hooks.slack.com")
        self.assertEqual(connection.port, 443)
        self.assertEqual(recorder.only.requests[0]["headers"]["Host"], "hooks.slack.com")

    def test_the_tls_context_verifies_the_certificate_and_the_hostname(self):
        _, recorder = post()
        context = recorder.only.context

        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_the_timeout_reaches_the_connection(self):
        recorder = Recorder()
        client = sender(recorder)

        client.post(PAYLOAD, credential=WEBHOOK_CREDENTIAL, destination=WEBHOOK_DESTINATION, timeout_seconds=7.5)

        self.assertEqual(recorder.only.timeout, 7.5)

    def test_the_connection_is_closed_on_every_path(self):
        for label, recorder in (
            ("success", Recorder()),
            ("connect failure", Recorder(connect_error=ConnectionRefusedError("refused"))),
            ("request failure", Recorder(request_error=TimeoutError("timeout"))),
        ):
            with self.subTest(label=label):
                post(recorder)
                self.assertTrue(recorder.only.closed)

    def test_only_the_exact_approved_hostname_is_accepted(self):
        cases = {
            "unapproved host": "https://evil.example/services/A/B/C",
            "subdomain of an approved host": "https://x.hooks.slack.com/services/A/B/C",
            "approved host as a prefix": "https://hooks.slack.com.evil.example/services/A/B/C",
        }
        for label, url in cases.items():
            with self.subTest(label=label):
                response, recorder = post(credential=SlackCredential(WEBHOOK, url))

                self.assertIs(response.error_class, TransportError.MALFORMED_URL)
                self.assertEqual(recorder.connections, [])

    def test_an_uppercase_hostname_is_refused_as_the_url_contracts_require(self):
        """Consistent with `validate_feed_url`, which requires a lowercase host."""

        response, recorder = post(
            credential=SlackCredential(WEBHOOK, "https://HOOKS.SLACK.COM/services/A/B/C"),
        )

        self.assertIs(response.error_class, TransportError.MALFORMED_URL)
        self.assertEqual(recorder.connections, [])

    def test_an_explicit_non_default_port_is_refused(self):
        response, recorder = post(
            credential=SlackCredential(WEBHOOK, "https://hooks.slack.com:8443/services/A/B/C"),
        )

        self.assertIs(response.error_class, TransportError.MALFORMED_URL)
        self.assertEqual(recorder.connections, [])

    def test_an_explicit_default_port_still_connects_to_443(self):
        response, recorder = post(
            credential=SlackCredential(WEBHOOK, "https://hooks.slack.com:443/services/A/B/C"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(recorder.only.port, 443)

    def test_credentials_in_the_url_are_refused(self):
        for url in (
            "https://user:pw@hooks.slack.com/services/A/B/C",
            "https://@hooks.slack.com/services/A/B/C",
        ):
            with self.subTest(url_form="user info"):
                response, recorder = post(credential=SlackCredential(WEBHOOK, url))

                self.assertIs(response.error_class, TransportError.MALFORMED_URL)
                self.assertEqual(recorder.connections, [])

    def test_an_unexpected_slack_path_is_refused(self):
        cases = {
            "wrong prefix": "https://hooks.slack.com/hooks/A/B/C",
            "too few segments": "https://hooks.slack.com/services/A/B",
            "too many segments": "https://hooks.slack.com/services/A/B/C/D",
            "empty segment": "https://hooks.slack.com/services/A//C",
            "root": "https://hooks.slack.com/",
            "fragment": "https://hooks.slack.com/services/A/B/C#x",
            "unencoded space": "https://hooks.slack.com/services/A/B/C D",
            "non-ascii": "https://hooks.slack.com/services/A/B/Cé",
            "malformed escape": "https://hooks.slack.com/services/A/B/C%ZZ",
        }
        for label, url in cases.items():
            with self.subTest(label=label):
                response, recorder = post(credential=SlackCredential(WEBHOOK, url))

                self.assertIs(response.error_class, TransportError.MALFORMED_URL)
                self.assertEqual(recorder.connections, [])

    def test_a_redirect_is_refused_without_being_followed(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                recorder = Recorder(
                    response=FakeResponse(status, b"", {"Location": "https://evil.example/steal"}),
                )
                response, _ = post(recorder)

                self.assertEqual(response.status_code, status)
                self.assertEqual(len(recorder.connections), 1, "a redirect must not open a second connection")
                self.assertEqual(len(recorder.only.requests), 1)
                self.assertEqual(_classify(response, max_retry_after_seconds=900)[0], FAILED_TERMINAL)

    def test_a_public_address_succeeds(self):
        for address in (PUBLIC_ADDRESS, PUBLIC_ADDRESS_ALT, PUBLIC_ADDRESS_V6):
            with self.subTest(address=address):
                response, recorder = post(resolver=fixed_resolver(address))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(recorder.only.address, address)

    def test_documentation_ranges_are_refused_like_any_other_reserved_space(self):
        """`is_global` covers reserved ranges the named flags do not."""

        for address in ("203.0.113.10", "198.51.100.7", "2001:db8:1::1"):
            with self.subTest(address=address):
                response, recorder = post(resolver=fixed_resolver(address))

                self.assertIs(response.error_class, TransportError.MALFORMED_URL)
                self.assertEqual(recorder.connections, [])

    def test_special_purpose_addresses_are_refused(self):
        cases = {
            "loopback": "127.0.0.1",
            "private": "10.0.0.5",
            "link local": "169.254.169.254",
            "multicast": "224.0.0.1",
            "unspecified": "0.0.0.0",
            "ipv6 loopback": "::1",
            "ipv6 link local": "fe80::1",
            "ipv6 unique local": "fc00::1",
            "carrier grade nat": "100.64.0.1",
        }
        for label, address in cases.items():
            with self.subTest(label=label):
                response, recorder = post(resolver=fixed_resolver(address))

                self.assertIs(response.error_class, TransportError.MALFORMED_URL)
                self.assertIs(response.bytes_sent, False)
                self.assertEqual(recorder.connections, [])

    def test_a_mixed_answer_set_is_refused_outright(self):
        """One private record poisons the whole answer, not just its own entry."""

        for addresses in (
            (PUBLIC_ADDRESS, "127.0.0.1"),
            ("127.0.0.1", PUBLIC_ADDRESS),
            (PUBLIC_ADDRESS, PUBLIC_ADDRESS_ALT, "10.1.2.3"),
        ):
            with self.subTest(addresses=addresses):
                response, recorder = post(resolver=fixed_resolver(*addresses))

                self.assertIs(response.error_class, TransportError.MALFORMED_URL)
                self.assertEqual(recorder.connections, [])

    def test_an_empty_answer_set_is_refused(self):
        response, recorder = post(resolver=fixed_resolver())

        self.assertIs(response.error_class, TransportError.MALFORMED_URL)
        self.assertEqual(recorder.connections, [])

    def test_resolution_happens_once_per_call_immediately_before_connecting(self):
        calls = []

        def resolve(hostname, port):
            calls.append((hostname, port))
            return [PUBLIC_ADDRESS]

        post(resolver=resolve)

        self.assertEqual(calls, [("hooks.slack.com", 443)])

    def test_a_webhook_body_is_never_read(self):
        """A webhook answers plain text, so the status is the whole outcome.

        Not reading it is what makes the next test possible: a body that cannot
        be read cannot discard a status that already decided the delivery.
        """

        recorder = Recorder(response=FakeResponse(200, b"ok"))
        post(recorder)

        self.assertEqual(recorder.only.response.read_calls, [])

    def test_a_definite_status_survives_an_unreadable_body(self):
        """The repaired defect: a slow body used to erase a received status.

        Every row here is a status Slack actually returned. Reading a body first
        turned a webhook 200 into `delivery_unknown` — sending an operator to
        inspect a message Slack had accepted — and stripped a 429 of its
        `Retry-After` and its retryable classification.
        """

        failures = {
            "timeout": TimeoutError("read timed out"),
            "connection reset": ConnectionResetError("reset"),
            "socket error": OSError("boom"),
        }
        statuses = {
            200: POSTED,
            429: FAILED_RETRYABLE,
            500: FAILED_RETRYABLE,
            503: FAILED_RETRYABLE,
            403: FAILED_TERMINAL,
        }
        for status, expected_state in statuses.items():
            for label, error in failures.items():
                with self.subTest(status=status, failure=label):
                    recorder = Recorder(response=FakeResponse(status, read_error=error))
                    response, _ = post(recorder)

                    self.assertEqual(response.status_code, status)
                    self.assertIsNone(response.error_class)
                    self.assertIs(response.bytes_sent, True)
                    self.assertEqual(_classify(response, max_retry_after_seconds=900)[0], expected_state)

    def test_a_definite_status_survives_an_oversized_body(self):
        for status in (200, 429, 500, 403):
            with self.subTest(status=status):
                recorder = Recorder(response=FakeResponse(status, b"a" * (MAX_SLACK_RESPONSE_BYTES * 4)))
                response, _ = post(recorder)

                self.assertEqual(response.status_code, status)
                self.assertIsNone(response.error_class)

    def test_a_rate_limit_keeps_its_retry_after_despite_an_unreadable_body(self):
        recorder = Recorder(
            response=FakeResponse(429, b"", {"Retry-After": "45"}, read_error=TimeoutError("slow")),
        )
        response, _ = post(recorder)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.retry_after_seconds, 45)
        self.assertEqual(_classify(response, max_retry_after_seconds=900), (FAILED_RETRYABLE, "http_429", 45))

    def test_retry_after_is_reported_as_an_integer_fact(self):
        recorder = Recorder(response=FakeResponse(429, b"", {"Retry-After": "42"}))
        response, _ = post(recorder)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.retry_after_seconds, 42)

    def test_retry_after_is_not_bounded_by_the_adapter(self):
        """`max_retry_after_seconds` is release policy the adapter never sees."""

        recorder = Recorder(response=FakeResponse(429, b"", {"Retry-After": "99999"}))
        response, _ = post(recorder)

        self.assertEqual(response.retry_after_seconds, 99999)
        self.assertEqual(_classify(response, max_retry_after_seconds=900), (FAILED_RETRYABLE, "http_429", 900))

    def test_a_malformed_retry_after_is_absent_rather_than_guessed(self):
        cases = ("", "   ", "soon", "-5", "1.5", "Wed, 21 Oct 2026 07:28:00 GMT", "12abc", "0x10")
        for raw in cases:
            with self.subTest(raw=raw):
                recorder = Recorder(response=FakeResponse(429, b"", {"Retry-After": raw}))
                response, _ = post(recorder)

                self.assertIsNone(response.retry_after_seconds)

    def test_a_zero_retry_after_is_reported_and_the_worker_discards_it(self):
        recorder = Recorder(response=FakeResponse(429, b"", {"Retry-After": "0"}))
        response, _ = post(recorder)

        self.assertEqual(response.retry_after_seconds, 0)
        self.assertIsNone(_classify(response, max_retry_after_seconds=900)[2])

    def test_a_webhook_outcome_carries_no_body_derived_fields(self):
        recorder = Recorder(response=FakeResponse(200, b'{"ok":false,"error":"channel_not_found"}'))
        response, _ = post(recorder)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.slack_error)
        self.assertIsNone(response.message_ts)
        self.assertEqual(_classify(response, max_retry_after_seconds=900)[0], POSTED)


class BotRequestTests(unittest.TestCase):
    def bot_post(self, recorder=None, payload=None, **kwargs):
        recorder = recorder if recorder is not None else Recorder()
        client = sender(recorder, kwargs.pop("resolver", None), **kwargs)
        response = client.post(
            payload if payload is not None else PAYLOAD,
            credential=BOT_CREDENTIAL,
            destination=BOT_DESTINATION,
            timeout_seconds=10.0,
        )
        return response, recorder

    def test_the_endpoint_is_the_fixed_post_message_method(self):
        response, recorder = self.bot_post(Recorder(response=FakeResponse(200, b'{"ok":true,"ts":"1.2"}')))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(recorder.only.hostname, SLACK_API_HOST)
        self.assertEqual(recorder.only.requests[0]["path"], SLACK_POST_MESSAGE_PATH)
        self.assertEqual(recorder.only.requests[0]["headers"]["Host"], SLACK_API_HOST)
        self.assertEqual(recorder.only.port, 443)

    def test_the_endpoint_does_not_depend_on_the_credential_or_the_payload(self):
        response, recorder = self.bot_post(
            Recorder(response=FakeResponse(200, b'{"ok":true,"ts":"1.2"}')),
            payload={**PAYLOAD, "url": "https://evil.example/api", "api_url": "https://evil.example"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(recorder.only.hostname, SLACK_API_HOST)
        self.assertEqual(recorder.only.requests[0]["path"], SLACK_POST_MESSAGE_PATH)

    def test_the_channel_comes_from_the_destination(self):
        _, recorder = self.bot_post(Recorder(response=FakeResponse(200, b'{"ok":true,"ts":"1.2"}')))
        body = json.loads(recorder.only.requests[0]["body"].decode("utf-8"))

        self.assertEqual(body["channel"], BOT_CHANNEL_ID)

    def test_the_payload_cannot_replace_the_channel(self):
        """The release route is the only authority for where a message goes."""

        _, recorder = self.bot_post(
            Recorder(response=FakeResponse(200, b'{"ok":true,"ts":"1.2"}')),
            payload={**PAYLOAD, "channel": "C0ATTACKER"},
        )
        body = json.loads(recorder.only.requests[0]["body"].decode("utf-8"))

        self.assertEqual(body["channel"], BOT_CHANNEL_ID)
        self.assertNotIn("C0ATTACKER", recorder.only.requests[0]["body"].decode("utf-8"))

    def test_the_token_is_sent_only_as_the_bearer_authorization_value(self):
        _, recorder = self.bot_post(Recorder(response=FakeResponse(200, b'{"ok":true,"ts":"1.2"}')))
        request = recorder.only.requests[0]

        self.assertEqual(request["headers"]["Authorization"], f"Bearer {BOT_TOKEN_VALUE}")
        self.assertNotIn(BOT_TOKEN_VALUE, request["body"].decode("utf-8"))
        self.assertNotIn(BOT_TOKEN_VALUE, request["path"])
        other_headers = {name: value for name, value in request["headers"].items() if name != "Authorization"}
        self.assertNotIn(BOT_TOKEN_VALUE, json.dumps(other_headers))

    def test_a_value_that_is_not_a_bot_token_never_reaches_a_socket(self):
        """Defence at the boundary; `process_delivery` already refused it once."""

        for token in (
            "has space\ttab\x00null",
            "line\rbreak",
            "curly’quote",
            WEBHOOK_URL,
            "xoxp-123456789012-abcdef",
            "xoxb-short",
            "",
        ):
            with self.subTest(token=token[:16]):
                recorder = Recorder()
                client = sender(recorder)
                response = client.post(
                    PAYLOAD,
                    credential=SlackCredential(BOT_TOKEN, token),
                    destination=BOT_DESTINATION,
                    timeout_seconds=10.0,
                )

                self.assertIs(response.error_class, TransportError.MALFORMED_URL)
                self.assertIs(response.bytes_sent, False)
                self.assertEqual(recorder.connections, [])

    def test_a_successful_body_yields_the_message_timestamp(self):
        recorder = Recorder(response=FakeResponse(200, b'{"ok":true,"ts":"1731440000.123456","channel":"C0ALERTS"}'))
        response, _ = recorder, None
        response, _ = self.bot_post(recorder)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.slack_error)
        self.assertEqual(response.message_ts, "1731440000.123456")
        self.assertEqual(_classify(response, max_retry_after_seconds=900)[0], POSTED)

    def test_a_success_without_a_usable_timestamp_still_posts(self):
        for body in (b'{"ok":true}', b'{"ok":true,"ts":null}', b'{"ok":true,"ts":"not-a-ts"}'):
            with self.subTest(body=body):
                response, _ = self.bot_post(Recorder(response=FakeResponse(200, body)))

                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.message_ts)
                self.assertEqual(_classify(response, max_retry_after_seconds=900)[0], POSTED)

    def test_ok_false_under_http_200_is_reported_as_a_bounded_error_code(self):
        cases = {
            b'{"ok":false,"error":"channel_not_found"}': ("channel_not_found", FAILED_TERMINAL),
            b'{"ok":false,"error":"invalid_auth"}': ("invalid_auth", FAILED_TERMINAL),
            b'{"ok":false,"error":"ratelimited"}': ("ratelimited", FAILED_RETRYABLE),
            b'{"ok":false,"error":"internal_error"}': ("internal_error", FAILED_RETRYABLE),
        }
        for body, (code, state) in cases.items():
            with self.subTest(body=body):
                response, _ = self.bot_post(Recorder(response=FakeResponse(200, body)))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.slack_error, code)
                self.assertEqual(_classify(response, max_retry_after_seconds=900)[0], state)

    def test_an_unbounded_error_member_becomes_a_fixed_code(self):
        cases = (
            b'{"ok":false,"error":"' + b"a" * 200 + b'"}',
            b'{"ok":false,"error":"Not A Code With Spaces"}',
            b'{"ok":false,"error":123}',
            b'{"ok":false}',
            b'{"ok":false,"error":""}',
        )
        for body in cases:
            with self.subTest(body=body[:40]):
                response, _ = self.bot_post(Recorder(response=FakeResponse(200, body)))

                self.assertEqual(response.slack_error, "unknown_error")
                self.assertEqual(_classify(response, max_retry_after_seconds=900)[0], FAILED_TERMINAL)

    def test_http_429_carries_retry_after(self):
        recorder = Recorder(response=FakeResponse(429, b'{"ok":false,"error":"ratelimited"}', {"Retry-After": "30"}))
        response, _ = self.bot_post(recorder)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.retry_after_seconds, 30)
        # A non-200 is decided by its status, so the body is not parsed.
        self.assertIsNone(response.slack_error)
        self.assertEqual(_classify(response, max_retry_after_seconds=900), (FAILED_RETRYABLE, "http_429", 30))

    def test_a_redirect_is_refused_without_being_followed(self):
        recorder = Recorder(response=FakeResponse(302, b"", {"Location": "https://evil.example/api"}))
        response, _ = self.bot_post(recorder)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(recorder.connections), 1)
        self.assertEqual(_classify(response, max_retry_after_seconds=900)[0], FAILED_TERMINAL)

    def test_a_malformed_json_body_under_200_is_unknown_rather_than_posted(self):
        """An unread `ok:false` must never be classified as success."""

        cases = (
            b"not json at all",
            b"",
            b"[]",
            b'{"ok":"true"}',
            b'{"okay":true}',
            b"{",
            b"\xff\xfe\x00",
        )
        for body in cases:
            with self.subTest(body=body[:20]):
                response, _ = self.bot_post(Recorder(response=FakeResponse(200, body)))

                self.assertIs(response.error_class, TransportError.READ_FAILED)
                self.assertIs(response.bytes_sent, True)
                self.assertEqual(_classify(response, max_retry_after_seconds=900)[0], DELIVERY_UNKNOWN)

    def test_the_body_read_is_bounded_by_one_byte_past_the_limit(self):
        recorder = Recorder(response=FakeResponse(200, b'{"ok":true,"ts":"1.2"}'))
        self.bot_post(recorder)

        self.assertEqual(recorder.only.response.read_calls, [MAX_SLACK_RESPONSE_BYTES + 1])

    def test_a_body_that_exactly_fills_the_limit_is_parsed(self):
        padding = b"a" * (MAX_SLACK_RESPONSE_BYTES - len(b'{"ok":true,"ts":"1.2","pad":""}'))
        body = b'{"ok":true,"ts":"1.2","pad":"' + padding + b'"}'
        self.assertEqual(len(body), MAX_SLACK_RESPONSE_BYTES)

        response, _ = self.bot_post(Recorder(response=FakeResponse(200, body)))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.message_ts, "1.2")

    def test_a_read_failure_under_200_is_unknown_rather_than_posted(self):
        """The one status whose body decides, so an unread body is ambiguous."""

        for error in (TimeoutError("slow"), ConnectionResetError("reset"), OSError("boom")):
            with self.subTest(error=type(error).__name__):
                response, _ = self.bot_post(Recorder(response=FakeResponse(200, read_error=error)))

                self.assertIsNotNone(response.error_class)
                self.assertIs(response.bytes_sent, True)
                self.assertEqual(_classify(response, max_retry_after_seconds=900)[0], DELIVERY_UNKNOWN)

    def test_a_non_200_status_is_decided_without_reading_the_body(self):
        for status in (429, 500, 403, 302):
            with self.subTest(status=status):
                recorder = Recorder(response=FakeResponse(status, read_error=TimeoutError("slow")))
                response, _ = self.bot_post(recorder)

                self.assertEqual(response.status_code, status)
                self.assertIsNone(response.error_class)
                self.assertEqual(recorder.only.response.read_calls, [])

    def test_an_oversized_body_under_200_is_unknown(self):
        body = b'{"ok":true,"ts":"1.2","pad":"' + b"a" * MAX_SLACK_RESPONSE_BYTES + b'"}'
        response, _ = self.bot_post(Recorder(response=FakeResponse(200, body)))

        self.assertIs(response.error_class, TransportError.READ_FAILED)
        self.assertEqual(_classify(response, max_retry_after_seconds=900)[0], DELIVERY_UNKNOWN)

    def test_the_bot_endpoint_is_dns_pinned_like_the_webhook_endpoint(self):
        response, recorder = self.bot_post(
            Recorder(response=FakeResponse(200, b'{"ok":true,"ts":"1.2"}')),
            resolver=fixed_resolver(PUBLIC_ADDRESS),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(recorder.only.address, PUBLIC_ADDRESS)
        self.assertEqual(recorder.only.hostname, SLACK_API_HOST)

    def test_a_special_purpose_answer_for_the_api_host_is_refused(self):
        response, recorder = self.bot_post(resolver=fixed_resolver("169.254.169.254"))

        self.assertIs(response.error_class, TransportError.MALFORMED_URL)
        self.assertEqual(recorder.connections, [])


class TransportSetupFailureTests(unittest.TestCase):
    """Failures before a socket exists are reported, never raised.

    Left uncaught these escaped `post`, and `process_delivery`'s blanket handler
    recorded `sender_raised` and `delivery_unknown` — the state that tells an
    operator to go and check Slack by hand for a request that never reached a
    socket. Every row below must be a reported fact with `bytes_sent=False`.
    """

    def test_a_tls_context_failure_is_reported_as_tls_failed(self):
        def broken_context():
            raise ssl.SSLError("cannot load the trust store")

        recorder = Recorder()
        client = SlackHttpSender(
            resolver=fixed_resolver(PUBLIC_ADDRESS),
            connection_factory=recorder,
            ssl_context_factory=broken_context,
        )

        response = client.post(
            PAYLOAD, credential=WEBHOOK_CREDENTIAL, destination=WEBHOOK_DESTINATION, timeout_seconds=10.0
        )

        self.assertIs(response.error_class, TransportError.TLS_FAILED)
        self.assertIs(response.bytes_sent, False)
        self.assertIsNone(response.status_code)
        self.assertEqual(recorder.connections, [], "no connection may be constructed without a context")

    def test_a_certificate_store_failure_is_reported_as_tls_failed(self):
        for error in (FileNotFoundError("no ca bundle"), OSError("permission denied"), ValueError("bad option")):
            with self.subTest(error=type(error).__name__):

                def broken_context(error=error):
                    raise error

                client = SlackHttpSender(
                    resolver=fixed_resolver(PUBLIC_ADDRESS),
                    connection_factory=Recorder(),
                    ssl_context_factory=broken_context,
                )

                response = client.post(
                    PAYLOAD, credential=WEBHOOK_CREDENTIAL, destination=WEBHOOK_DESTINATION, timeout_seconds=10.0
                )

                self.assertIs(response.error_class, TransportError.TLS_FAILED)
                self.assertIs(response.bytes_sent, False)

    def test_a_connection_construction_failure_is_reported_as_connect_failed(self):
        def broken_factory(hostname, address, port, timeout, context):
            raise OSError("cannot allocate a connection")

        client = SlackHttpSender(
            resolver=fixed_resolver(PUBLIC_ADDRESS),
            connection_factory=broken_factory,
            ssl_context_factory=ssl.create_default_context,
        )

        response = client.post(
            PAYLOAD, credential=WEBHOOK_CREDENTIAL, destination=WEBHOOK_DESTINATION, timeout_seconds=10.0
        )

        self.assertIs(response.error_class, TransportError.CONNECT_FAILED)
        self.assertIs(response.bytes_sent, False)
        self.assertIsNone(response.status_code)

    def test_setup_failures_carry_no_exception_detail(self):
        marker = "TRUST-STORE-PATH-THAT-MUST-NOT-LEAK"

        def broken_context():
            raise ssl.SSLError(marker)

        client = SlackHttpSender(
            resolver=fixed_resolver(PUBLIC_ADDRESS),
            connection_factory=Recorder(),
            ssl_context_factory=broken_context,
        )

        response = client.post(
            PAYLOAD, credential=WEBHOOK_CREDENTIAL, destination=WEBHOOK_DESTINATION, timeout_seconds=10.0
        )

        self.assertNotIn(marker, repr(response))

    def test_setup_failures_never_raise(self):
        for label, factory, context in (
            ("tls", Recorder(), lambda: (_ for _ in ()).throw(ssl.SSLError("x"))),
            ("connection", lambda *a: (_ for _ in ()).throw(OSError("x")), ssl.create_default_context),
        ):
            with self.subTest(label=label):
                client = SlackHttpSender(
                    resolver=fixed_resolver(PUBLIC_ADDRESS),
                    connection_factory=factory,
                    ssl_context_factory=context,
                )
                try:
                    client.post(
                        PAYLOAD,
                        credential=WEBHOOK_CREDENTIAL,
                        destination=WEBHOOK_DESTINATION,
                        timeout_seconds=10.0,
                    )
                except Exception as error:  # pragma: no cover - a raise is the failure
                    self.fail(f"{label} setup failure raised instead of reporting: {error!r}")

    def test_a_close_failure_does_not_mask_the_outcome(self):
        class UnclosableConnection(FakeConnection):
            def close(self):
                raise OSError("close failed")

        class UnclosableRecorder(Recorder):
            def __call__(self, hostname, address, port, timeout, context):
                connection = UnclosableConnection(hostname, address, port, timeout, context)
                for name, value in self.behaviour.items():
                    setattr(connection, name, value)
                self.connections.append(connection)
                return connection

        recorder = UnclosableRecorder(response=FakeResponse(200, b"ok"))
        client = sender(recorder)

        response = client.post(
            PAYLOAD, credential=WEBHOOK_CREDENTIAL, destination=WEBHOOK_DESTINATION, timeout_seconds=10.0
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.error_class)


class RedactionTests(unittest.TestCase):
    """No credential, URL, or response body reaches a return value or a log."""

    def scenarios(self):
        return {
            "webhook success": (Recorder(response=FakeResponse(200, b"ok")), WEBHOOK_CREDENTIAL, WEBHOOK_DESTINATION),
            "webhook unapproved host": (
                Recorder(),
                SlackCredential(WEBHOOK, WEBHOOK_URL.replace("hooks.slack.com", "evil.example")),
                WEBHOOK_DESTINATION,
            ),
            "webhook bad path": (
                Recorder(),
                SlackCredential(WEBHOOK, f"https://hooks.slack.com/nope/{WEBHOOK_SECRET_SEGMENT}"),
                WEBHOOK_DESTINATION,
            ),
            "webhook connect failure": (
                Recorder(connect_error=ConnectionRefusedError("refused")),
                WEBHOOK_CREDENTIAL,
                WEBHOOK_DESTINATION,
            ),
            "webhook tls failure": (
                Recorder(connect_error=ssl.SSLCertVerificationError("mismatch")),
                WEBHOOK_CREDENTIAL,
                WEBHOOK_DESTINATION,
            ),
            "webhook error status": (
                Recorder(response=FakeResponse(403, b"invalid_token PLACEHOLDER-BODY")),
                WEBHOOK_CREDENTIAL,
                WEBHOOK_DESTINATION,
            ),
            "bot success": (
                Recorder(response=FakeResponse(200, b'{"ok":true,"ts":"1.2"}')),
                BOT_CREDENTIAL,
                BOT_DESTINATION,
            ),
            "bot auth failure": (
                Recorder(response=FakeResponse(200, b'{"ok":false,"error":"invalid_auth"}')),
                BOT_CREDENTIAL,
                BOT_DESTINATION,
            ),
            "bot malformed body": (
                Recorder(response=FakeResponse(200, b"PLACEHOLDER-BODY not json")),
                BOT_CREDENTIAL,
                BOT_DESTINATION,
            ),
        }

    def test_no_returned_value_or_log_record_carries_a_secret(self):
        capture = LogCapture(self)
        secrets = (WEBHOOK_URL, WEBHOOK_SECRET_SEGMENT, BOT_TOKEN_VALUE, "PLACEHOLDER-BODY")

        for label, (recorder, credential, destination) in self.scenarios().items():
            with self.subTest(label=label):
                client = sender(recorder)
                response = client.post(PAYLOAD, credential=credential, destination=destination, timeout_seconds=10.0)
                rendered = f"{response!r}"
                for secret in secrets:
                    self.assertNotIn(secret, rendered)

        self.assertEqual(capture.records, [], "the transport must not log")
        for secret in secrets:
            self.assertNotIn(secret, capture.text)

    def test_no_raised_exception_carries_a_secret(self):
        """Every failure is returned as a fact; nothing escapes as an exception."""

        for label, (recorder, credential, destination) in self.scenarios().items():
            with self.subTest(label=label):
                client = sender(recorder)
                try:
                    client.post(PAYLOAD, credential=credential, destination=destination, timeout_seconds=10.0)
                except Exception as error:  # pragma: no cover - a raise is itself the failure
                    self.fail(f"{label} raised instead of reporting: {error!r}")

    def test_a_traceback_from_the_adapter_would_not_print_the_webhook_url(self):
        buffer = io.StringIO()
        client = sender(Recorder())

        response = client.post(
            PAYLOAD,
            credential=SlackCredential(WEBHOOK, "https://evil.example/services/A/B/NotARealWebhookSecret0000"),
            destination=WEBHOOK_DESTINATION,
            timeout_seconds=10.0,
        )
        print(repr(response), file=buffer)

        self.assertNotIn(WEBHOOK_SECRET_SEGMENT, buffer.getvalue())
        self.assertNotIn("evil.example", buffer.getvalue())


class WorkerBoundaryTests(WorkerFixture):
    """A release-backed delivery through the real adapter to worker classification.

    This is the integration boundary the slice needs: the worker derives the
    destination from the exact inventory release, the real `SlackHttpSender`
    builds and reports the request over a fake connection, and the worker turns
    the reported facts into a durable outcome. Webhook and bot routes are
    exercised separately because their handoff shapes differ.
    """

    def transport(self, recorder):
        return SlackHttpSender(
            resolver=fixed_resolver(PUBLIC_ADDRESS),
            connection_factory=recorder,
            ssl_context_factory=ssl.create_default_context,
        )

    def test_a_webhook_route_posts_through_the_real_adapter(self):
        recorder = Recorder(response=FakeResponse(200, b"ok"))
        self.credentials = type(self.credentials)({self.route["credential_secret_id"]: WEBHOOK_CREDENTIAL})
        self.queued_record()

        result = self.process(sender=self.transport(recorder))

        self.assertTrue(result.handled)
        self.assertEqual(result.state, POSTED)
        self.assertTrue(result.performed_network_call)
        connection = recorder.only
        self.assertEqual(connection.hostname, "hooks.slack.com")
        self.assertEqual(connection.address, PUBLIC_ADDRESS)
        request = connection.requests[0]
        self.assertEqual(request["headers"]["Host"], "hooks.slack.com")
        body = json.loads(request["body"].decode("utf-8"))
        # The rendered fallback, carried verbatim. A webhook post has no
        # channel: the URL fixes it.
        title = self.candidate["announcement"]["title"]
        self.assertIn(title, body["text"])
        self.assertNotIn("channel", body)
        self.assertEqual(self.record().slack_response["response_class"], "http_200")

    def test_a_webhook_route_maps_a_rate_limit_to_a_scheduled_retry(self):
        recorder = Recorder(response=FakeResponse(429, b"", {"Retry-After": "45"}))
        self.credentials = type(self.credentials)({self.route["credential_secret_id"]: WEBHOOK_CREDENTIAL})
        self.queued_record()

        result = self.process(sender=self.transport(recorder))

        self.assertEqual(result.state, FAILED_RETRYABLE)
        record = self.record()
        self.assertEqual(record.slack_response["response_class"], "http_429")
        self.assertEqual(record.slack_response["retry_after_seconds"], 45)
        self.assertIsNotNone(record.next_action_at)

    def test_a_webhook_transport_failure_before_the_write_is_retryable(self):
        recorder = Recorder(connect_error=ConnectionRefusedError("refused"))
        self.credentials = type(self.credentials)({self.route["credential_secret_id"]: WEBHOOK_CREDENTIAL})
        self.queued_record()

        result = self.process(sender=self.transport(recorder))

        self.assertEqual(result.state, FAILED_RETRYABLE)
        self.assertIs(self.record().slack_response["bytes_sent"], False)
        self.assertEqual(self.record().slack_response["response_class"], "transport_connect_failed")

    def test_a_webhook_ambiguous_failure_becomes_delivery_unknown(self):
        recorder = Recorder(request_error=TimeoutError("read timed out"))
        self.credentials = type(self.credentials)({self.route["credential_secret_id"]: WEBHOOK_CREDENTIAL})
        self.queued_record()

        result = self.process(sender=self.transport(recorder))

        self.assertEqual(result.state, DELIVERY_UNKNOWN)
        self.assertIs(self.record().slack_response["bytes_sent"], True)
        self.assertIsNone(self.record().expires_at)

    def test_a_tls_setup_failure_is_not_sender_raised_or_unknown(self):
        """Finding 5 at the boundary that made it matter.

        Uncaught, this escaped `post` and `process_delivery`'s blanket handler
        recorded `sender_raised` and `delivery_unknown` — the state that tells an
        operator to inspect Slack by hand — for a request that never reached a
        socket. It must be a reported no-bytes fact, and therefore retryable.
        """

        def broken_context():
            raise ssl.SSLError("cannot load the trust store")

        recorder = Recorder()
        transport = SlackHttpSender(
            resolver=fixed_resolver(PUBLIC_ADDRESS),
            connection_factory=recorder,
            ssl_context_factory=broken_context,
        )
        self.credentials = type(self.credentials)({self.route["credential_secret_id"]: WEBHOOK_CREDENTIAL})
        self.queued_record()

        result = self.process(sender=transport)

        self.assertEqual(result.state, FAILED_RETRYABLE)
        self.assertNotEqual(result.state, DELIVERY_UNKNOWN)
        record = self.record()
        self.assertEqual(record.slack_response["response_class"], "transport_tls_failed")
        self.assertNotEqual(record.slack_response["response_class"], "sender_raised")
        self.assertIs(record.slack_response["bytes_sent"], False)
        self.assertEqual(recorder.connections, [])

    def test_a_connection_construction_failure_is_not_sender_raised_or_unknown(self):
        def broken_factory(hostname, address, port, timeout, context):
            raise OSError("cannot allocate a connection")

        transport = SlackHttpSender(
            resolver=fixed_resolver(PUBLIC_ADDRESS),
            connection_factory=broken_factory,
            ssl_context_factory=ssl.create_default_context,
        )
        self.credentials = type(self.credentials)({self.route["credential_secret_id"]: WEBHOOK_CREDENTIAL})
        self.queued_record()

        result = self.process(sender=transport)

        self.assertEqual(result.state, FAILED_RETRYABLE)
        self.assertEqual(self.record().slack_response["response_class"], "transport_connect_failed")
        self.assertIs(self.record().slack_response["bytes_sent"], False)

    def test_a_webhook_status_survives_an_unreadable_body_end_to_end(self):
        """Finding 2 through the worker: the definite status decides the state."""

        for status, expected in ((200, POSTED), (429, FAILED_RETRYABLE), (403, FAILED_TERMINAL)):
            with self.subTest(status=status):
                self.store = type(self.store)()
                recorder = Recorder(
                    response=FakeResponse(status, b"", {"Retry-After": "30"}, read_error=TimeoutError("slow")),
                )
                self.credentials = type(self.credentials)({self.route["credential_secret_id"]: WEBHOOK_CREDENTIAL})
                self.queued_record()

                result = self.process(sender=self.transport(recorder))

                self.assertEqual(result.state, expected)
                self.assertEqual(self.record().slack_response["response_class"], f"http_{status}")
                self.assertEqual(self.record().network_attempt_count, 1)

    def bot_release(self):
        """Publish a bot-mode release and rebuild the record against it."""

        inventory = bot_mode_inventory(self.inventory)
        self._publish_release(inventory_body=json.dumps(inventory).encode())
        self._build_candidate_and_request()
        self.credentials = type(self.credentials)({BOT_TOKEN_SECRET_ID: BOT_CREDENTIAL})
        return inventory

    def test_a_bot_route_posts_the_release_channel_through_the_real_adapter(self):
        self.bot_release()
        recorder = Recorder(response=FakeResponse(200, b'{"ok":true,"ts":"1731440000.000100"}'))
        self.queued_record()

        result = self.process(sender=self.transport(recorder))

        self.assertTrue(result.handled)
        self.assertEqual(result.state, POSTED)
        connection = recorder.only
        self.assertEqual(connection.hostname, SLACK_API_HOST)
        request = connection.requests[0]
        self.assertEqual(request["path"], SLACK_POST_MESSAGE_PATH)
        self.assertEqual(request["headers"]["Authorization"], f"Bearer {BOT_TOKEN_VALUE}")
        body = json.loads(request["body"].decode("utf-8"))
        self.assertEqual(body["channel"], BOT_CHANNEL_ID)
        record = self.record()
        self.assertEqual(record.slack_response["message_ts"], "1731440000.000100")
        self.assertEqual(record.destination_key, f"t0acme-{BOT_CHANNEL_ID.casefold()}")

    def test_a_bot_route_maps_ok_false_to_a_terminal_outcome(self):
        self.bot_release()
        recorder = Recorder(response=FakeResponse(200, b'{"ok":false,"error":"channel_not_found"}'))
        self.queued_record()

        result = self.process(sender=self.transport(recorder))

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertEqual(self.record().slack_response["response_class"], "slack_channel_not_found")

    def test_a_bot_route_maps_an_unreadable_body_to_delivery_unknown(self):
        self.bot_release()
        recorder = Recorder(response=FakeResponse(200, b"not json"))
        self.queued_record()

        result = self.process(sender=self.transport(recorder))

        self.assertEqual(result.state, DELIVERY_UNKNOWN)
        self.assertEqual(self.record().slack_response["response_class"], "transport_read_failed")

    def assert_record_holds_no_credential(self):
        stored = json.dumps(self.record().slack_response)
        for secret in (WEBHOOK_URL, WEBHOOK_SECRET_SEGMENT, BOT_TOKEN_VALUE):
            self.assertNotIn(secret, stored)
        self.assertNotIn(WEBHOOK_URL, json.dumps(self.record().request))

    def test_a_webhook_delivery_record_carries_no_credential(self):
        self.credentials = type(self.credentials)({self.route["credential_secret_id"]: WEBHOOK_CREDENTIAL})
        self.queued_record()

        self.process(sender=self.transport(Recorder(response=FakeResponse(200, b"ok"))))

        self.assertEqual(self.record().status, POSTED)
        self.assert_record_holds_no_credential()

    def test_a_bot_delivery_record_carries_no_credential(self):
        self.bot_release()
        self.queued_record()

        self.process(sender=self.transport(Recorder(response=FakeResponse(200, b'{"ok":true,"ts":"1.2"}'))))

        self.assertEqual(self.record().status, POSTED)
        self.assert_record_holds_no_credential()


if __name__ == "__main__":
    unittest.main()
