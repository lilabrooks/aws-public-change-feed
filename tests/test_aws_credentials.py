"""The AWS-backed credential readers, one per accepted secret store.

`deployment.schema.json` accepts `secrets_manager` and `ssm_parameter_store`,
and `infra/central` provisions either container and grants the matching read
action, so both are deployable and both are covered here.
`test_both_accepted_secret_stores_have_an_adapter` reads the enum out of the
committed schema rather than restating it, so adding a third accepted store
fails this file instead of silently shipping without an adapter.

No test asserts a credential value, and none appears in a fixture. The value
used throughout is an obvious non-secret placeholder, and the redaction tests
search raised messages, `repr` output, and captured log records for it. That
inverts the usual habit: the interesting assertion is that the value is
*absent*, so the placeholder has to be distinctive enough for a substring
search to be meaningful.
"""

import io
import json
import logging
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

import boto3
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.aws_credentials import (  # noqa: E402
    MAX_CREDENTIAL_CHARACTERS,
    MAX_ERROR_CODE_CHARACTERS,
    MAX_SECRET_ID_CHARACTERS,
    SECRETS_MANAGER,
    SECURE_STRING,
    SSM_PARAMETER_STORE,
    UNKNOWN_ERROR_CODE,
    SecretsManagerCredentialReader,
    SsmParameterCredentialReader,
    _error_code,
    credential_reader_for,
)
from aws_public_change_feed.credentials import (  # noqa: E402
    BOT_TOKEN,
    MAX_BOT_TOKEN_CHARACTERS,
    WEBHOOK,
    CredentialAccessDenied,
    CredentialEmpty,
    CredentialError,
    CredentialNotFound,
    CredentialReadError,
    CredentialUnavailable,
    CredentialUnreadable,
    InvalidBotToken,
    SlackCredential,
    validate_bot_token,
)

SECRET_ID = "aws-public-change-alerting/slack/shared-alerts-webhook"

# Deliberately not a real credential shape, and distinctive so a substring
# search for it in messages, reprs, and logs proves absence rather than luck.
PLACEHOLDER_VALUE = "PLACEHOLDER-NOT-A-REAL-CREDENTIAL-0123456789"

# Structurally shaped like their real counterparts so the validators are
# exercised, and marked so nothing here can be mistaken for a live secret.
WEBHOOK_URL = "https://hooks.slack.com/services/TPLACEHOLDER/BPLACEHOLDER/NotARealWebhookSecret0000"
BOT_TOKEN_VALUE = "xoxb-PLACEHOLDER-NOT-A-REAL-TOKEN-0123456789"


class FakeClientError(Exception):
    """Stands in for `botocore.exceptions.ClientError`.

    The adapters read `response["Error"]["Code"]` defensively rather than
    importing `botocore`, so a stand-in exercises the same mapping without
    coupling these tests to the SDK's exception hierarchy.
    """

    def __init__(self, code, message="a provider message that must not be carried forward"):
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeSecretsManager:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def get_secret_value(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class FakeSsm:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def get_parameter(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


def secrets_reader(response=None, error=None, kind=WEBHOOK):
    client = FakeSecretsManager(response=response, error=error)
    return SecretsManagerCredentialReader(client, kind=kind), client


def ssm_reader(response=None, error=None, kind=WEBHOOK):
    client = FakeSsm(response=response, error=error)
    return SsmParameterCredentialReader(client, kind=kind), client


class StoreCoverageTests(unittest.TestCase):
    def test_both_accepted_secret_stores_have_an_adapter(self):
        """The implemented set is compared against the committed enum."""

        schema = json.loads((ROOT / "schemas" / "deployment.schema.json").read_text(encoding="utf-8"))
        accepted = set(schema["properties"]["secret_store"]["enum"])

        self.assertEqual(accepted, {SECRETS_MANAGER, SSM_PARAMETER_STORE})
        clients = {SECRETS_MANAGER: FakeSecretsManager, SSM_PARAMETER_STORE: FakeSsm}
        for store in accepted:
            with self.subTest(store=store):
                reader = credential_reader_for(store, clients[store](), kind=WEBHOOK)
                self.assertEqual(reader.store, store)

    def test_an_unknown_store_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unsupported secret_store"):
            credential_reader_for("vault", FakeSecretsManager(), kind=WEBHOOK)

    def test_the_reader_maps_each_store_to_its_own_adapter(self):
        self.assertIsInstance(
            credential_reader_for(SECRETS_MANAGER, FakeSecretsManager(), kind=WEBHOOK),
            SecretsManagerCredentialReader,
        )
        self.assertIsInstance(
            credential_reader_for(SSM_PARAMETER_STORE, FakeSsm(), kind=WEBHOOK),
            SsmParameterCredentialReader,
        )


class ConstructionTests(unittest.TestCase):
    """Constructor inputs are checked before any AWS call is possible."""

    def test_a_missing_client_is_refused(self):
        for reader in (SecretsManagerCredentialReader, SsmParameterCredentialReader):
            with self.subTest(reader=reader.__name__):
                with self.assertRaisesRegex(ValueError, "needs a client"):
                    reader(None, kind=WEBHOOK)

    def test_an_unconfigured_kind_is_refused(self):
        # `None` is included deliberately: a composition root that forgets to
        # read the release's delivery mode passes it, and the check must not
        # treat a falsy kind as "decide later".
        kinds: tuple[Any, ...] = ("", "webhook", "slack", None, "incoming_webhook_v2")
        for kind in kinds:
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "credential kind must be one of"):
                    SecretsManagerCredentialReader(FakeSecretsManager(), kind=kind)

    def test_a_client_without_the_required_operation_is_refused(self):
        """A swapped client fails at wiring time, not mid-delivery."""

        with self.assertRaisesRegex(ValueError, "get_secret_value"):
            SecretsManagerCredentialReader(FakeSsm(), kind=WEBHOOK)
        with self.assertRaisesRegex(ValueError, "get_parameter"):
            SsmParameterCredentialReader(FakeSecretsManager(), kind=WEBHOOK)

    def test_construction_makes_no_aws_call(self):
        client = FakeSecretsManager(response={"SecretString": PLACEHOLDER_VALUE})

        SecretsManagerCredentialReader(client, kind=WEBHOOK)

        self.assertEqual(client.calls, [])


class SecretsManagerTests(unittest.TestCase):
    def test_a_secret_string_is_returned_with_the_configured_kind(self):
        reader, client = secrets_reader(response={"SecretString": PLACEHOLDER_VALUE})

        credential = reader.read(SECRET_ID)

        self.assertIsInstance(credential, SlackCredential)
        self.assertEqual(credential.kind, WEBHOOK)
        self.assertEqual(credential.value, PLACEHOLDER_VALUE)
        self.assertEqual(client.calls, [{"SecretId": SECRET_ID}])

    def test_the_exact_identifier_is_sent_unmodified(self):
        reader, client = secrets_reader(response={"SecretString": PLACEHOLDER_VALUE})

        reader.read(SECRET_ID)

        self.assertEqual(client.calls[0]["SecretId"], SECRET_ID)
        self.assertNotIn("VersionStage", client.calls[0])
        self.assertNotIn("VersionId", client.calls[0])

    def test_the_configured_kind_is_preserved_for_bot_mode(self):
        reader, _ = secrets_reader(response={"SecretString": PLACEHOLDER_VALUE}, kind=BOT_TOKEN)

        self.assertEqual(reader.read(SECRET_ID).kind, BOT_TOKEN)

    def test_surrounding_whitespace_is_removed(self):
        """A trailing newline is an operator artifact, not a different secret."""

        reader, _ = secrets_reader(response={"SecretString": f"  {PLACEHOLDER_VALUE}\n"})

        self.assertEqual(reader.read(SECRET_ID).value, PLACEHOLDER_VALUE)

    def test_a_binary_only_secret_is_unreadable(self):
        reader, _ = secrets_reader(response={"SecretBinary": b"\x00\x01"})

        with self.assertRaises(CredentialUnreadable) as caught:
            reader.read(SECRET_ID)
        self.assertIn("binary", str(caught.exception))

    def test_an_empty_secret_string_is_empty_rather_than_unreadable(self):
        for stored in ("", "   ", "\n"):
            with self.subTest(stored=stored):
                reader, _ = secrets_reader(response={"SecretString": stored})
                with self.assertRaises(CredentialEmpty):
                    reader.read(SECRET_ID)

    def test_an_absent_secret_string_is_empty(self):
        reader, _ = secrets_reader(response={})

        with self.assertRaises(CredentialEmpty):
            reader.read(SECRET_ID)

    def test_a_non_mapping_response_is_unreadable(self):
        reader, _ = secrets_reader(response=["SecretString"])

        with self.assertRaises(CredentialUnreadable):
            reader.read(SECRET_ID)

    def test_a_non_text_secret_value_is_unreadable(self):
        reader, _ = secrets_reader(response={"SecretString": 12345})

        with self.assertRaises(CredentialUnreadable):
            reader.read(SECRET_ID)

    def test_a_value_with_a_line_break_is_unreadable(self):
        """A newline inside the value would split a header or a request line."""

        reader, _ = secrets_reader(response={"SecretString": f"{PLACEHOLDER_VALUE}\nX-Injected: 1"})

        with self.assertRaises(CredentialUnreadable):
            reader.read(SECRET_ID)

    def test_an_oversized_value_is_unreadable(self):
        reader, _ = secrets_reader(response={"SecretString": "a" * (MAX_CREDENTIAL_CHARACTERS + 1)})

        with self.assertRaises(CredentialUnreadable):
            reader.read(SECRET_ID)

    def test_a_missing_secret_is_not_found(self):
        reader, _ = secrets_reader(error=FakeClientError("ResourceNotFoundException"))

        with self.assertRaises(CredentialNotFound):
            reader.read(SECRET_ID)

    def test_a_denial_is_reported_as_a_denial(self):
        for code in ("AccessDeniedException", "AccessDenied", "UnauthorizedOperation"):
            with self.subTest(code=code):
                reader, _ = secrets_reader(error=FakeClientError(code))
                with self.assertRaises(CredentialAccessDenied):
                    reader.read(SECRET_ID)

    def test_a_reviewed_permanent_code_is_terminal(self):
        for code in ("ValidationException", "InvalidParameterException", "KMSAccessDeniedException"):
            with self.subTest(code=code):
                reader, _ = secrets_reader(error=FakeClientError(code))
                with self.assertRaises(CredentialReadError):
                    reader.read(SECRET_ID)


class SsmParameterTests(unittest.TestCase):
    def test_a_secure_string_is_returned_and_decryption_is_requested(self):
        reader, client = ssm_reader(response={"Parameter": {"Value": PLACEHOLDER_VALUE, "Type": "SecureString"}})

        credential = reader.read(SECRET_ID)

        self.assertEqual(credential.kind, WEBHOOK)
        self.assertEqual(credential.value, PLACEHOLDER_VALUE)
        self.assertEqual(client.calls, [{"Name": SECRET_ID, "WithDecryption": True}])

    def test_decryption_is_always_requested(self):
        """Without it the call returns ciphertext, which would look like a value."""

        reader, client = ssm_reader(response={"Parameter": {"Value": PLACEHOLDER_VALUE, "Type": SECURE_STRING}})

        reader.read(SECRET_ID)

        self.assertIs(client.calls[0]["WithDecryption"], True)

    def test_the_configured_kind_is_preserved(self):
        reader, _ = ssm_reader(
            response={"Parameter": {"Value": PLACEHOLDER_VALUE, "Type": SECURE_STRING}}, kind=BOT_TOKEN
        )

        self.assertEqual(reader.read(SECRET_ID).kind, BOT_TOKEN)

    def test_a_missing_parameter_is_not_found(self):
        for code in ("ParameterNotFound", "ParameterVersionNotFound"):
            with self.subTest(code=code):
                reader, _ = ssm_reader(error=FakeClientError(code))
                with self.assertRaises(CredentialNotFound):
                    reader.read(SECRET_ID)

    def test_a_denial_is_reported_as_a_denial(self):
        reader, _ = ssm_reader(error=FakeClientError("AccessDeniedException"))

        with self.assertRaises(CredentialAccessDenied):
            reader.read(SECRET_ID)

    def test_only_a_secure_string_is_accepted(self):
        """`WithDecryption` is ignored for a plaintext parameter, so Type decides."""

        for parameter_type in ("String", "StringList", "", None):
            with self.subTest(parameter_type=parameter_type):
                parameter: dict[str, Any] = {"Value": PLACEHOLDER_VALUE}
                if parameter_type is not None:
                    parameter["Type"] = parameter_type
                reader, _ = ssm_reader(response={"Parameter": parameter})
                with self.assertRaises(CredentialUnreadable) as caught:
                    reader.read(SECRET_ID)
                self.assertIn(SECURE_STRING, str(caught.exception))
                self.assertNotIn(PLACEHOLDER_VALUE, str(caught.exception))

    def test_a_string_list_holding_a_valid_looking_value_is_still_refused(self):
        """It parses. It was never encrypted, which is the whole objection."""

        reader, _ = ssm_reader(response={"Parameter": {"Value": WEBHOOK_URL, "Type": "StringList"}})

        with self.assertRaises(CredentialUnreadable):
            reader.read(SECRET_ID)

    def test_a_response_without_a_parameter_is_unreadable(self):
        for response in ({}, {"Parameter": None}, {"Parameter": "value"}, ["Parameter"]):
            with self.subTest(response=response):
                reader, _ = ssm_reader(response=response)
                with self.assertRaises(CredentialUnreadable):
                    reader.read(SECRET_ID)

    def test_an_empty_parameter_value_is_empty(self):
        reader, _ = ssm_reader(response={"Parameter": {"Value": "  ", "Type": SECURE_STRING}})

        with self.assertRaises(CredentialEmpty):
            reader.read(SECRET_ID)


class IdentifierTests(unittest.TestCase):
    def test_a_missing_identifier_is_refused_before_any_call(self):
        for identifier in ("", "   ", None, 17):
            with self.subTest(identifier=identifier):
                reader, client = secrets_reader(response={"SecretString": PLACEHOLDER_VALUE})
                with self.assertRaises(CredentialNotFound):
                    reader.read(identifier)
                self.assertEqual(client.calls, [])

    def test_an_over_long_identifier_is_refused_before_any_call(self):
        reader, client = secrets_reader(response={"SecretString": PLACEHOLDER_VALUE})

        with self.assertRaises(CredentialNotFound):
            reader.read("a" * (MAX_SECRET_ID_CHARACTERS + 1))
        self.assertEqual(client.calls, [])

    def test_every_permanent_failure_is_a_credential_read_error(self):
        """The worker resolves this half terminally, so every member shares the base."""

        for subclass in (CredentialNotFound, CredentialAccessDenied, CredentialEmpty, CredentialUnreadable):
            with self.subTest(subclass=subclass.__name__):
                self.assertTrue(issubclass(subclass, CredentialReadError))


class RedactionTests(unittest.TestCase):
    """No value, identifier, or provider message reaches a message, repr, or log."""

    def failures(self):
        """One reader per failure path, each carrying the placeholder value."""

        return [
            ("binary", *secrets_reader(response={"SecretBinary": PLACEHOLDER_VALUE.encode()})),
            ("line break", *secrets_reader(response={"SecretString": f"{PLACEHOLDER_VALUE}\nx"})),
            ("non-text", *secrets_reader(response={"SecretString": {"url": PLACEHOLDER_VALUE}})),
            ("oversized", *secrets_reader(response={"SecretString": PLACEHOLDER_VALUE * 400})),
            (
                "permanent provider error",
                *secrets_reader(error=FakeClientError("ValidationException", PLACEHOLDER_VALUE)),
            ),
            (
                "transient provider error",
                *secrets_reader(error=FakeClientError("ThrottlingException", PLACEHOLDER_VALUE)),
            ),
            (
                "ssm non-text",
                *ssm_reader(response={"Parameter": {"Value": [PLACEHOLDER_VALUE], "Type": SECURE_STRING}}),
            ),
            ("ssm transient error", *ssm_reader(error=FakeClientError("InternalServerError", PLACEHOLDER_VALUE))),
            ("ssm wrong type", *ssm_reader(response={"Parameter": {"Value": PLACEHOLDER_VALUE, "Type": "String"}})),
        ]

    def test_no_raised_message_contains_the_value_or_the_identifier(self):
        for label, reader, _ in self.failures():
            with self.subTest(label=label):
                with self.assertRaises(CredentialError) as caught:
                    reader.read(SECRET_ID)
                rendered = f"{caught.exception}{caught.exception!r}"
                self.assertNotIn(PLACEHOLDER_VALUE, rendered)
                self.assertNotIn(SECRET_ID, rendered)

    def test_no_chained_cause_carries_the_provider_message(self):
        """`from None` is deliberate: a chained ClientError would print it."""

        reader, _ = secrets_reader(error=FakeClientError("ThrottlingException", PLACEHOLDER_VALUE))

        with self.assertRaises(CredentialError) as caught:
            reader.read(SECRET_ID)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_no_log_record_contains_the_value(self):
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Capture()
        root = logging.getLogger()
        root.addHandler(handler)
        previous = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, previous)

        reader, _ = secrets_reader(response={"SecretString": PLACEHOLDER_VALUE})
        reader.read(SECRET_ID)
        for label, failing, _ in self.failures():
            with self.subTest(label=label):
                with self.assertRaises(CredentialError):
                    failing.read(SECRET_ID)

        self.assertEqual(records, [], "the credential adapters must not log")

    def test_the_credential_repr_redacts_its_value(self):
        credential = SlackCredential(WEBHOOK, PLACEHOLDER_VALUE)

        for rendered in (repr(credential), str(credential), f"{credential}", f"{credential!r}"):
            self.assertNotIn(PLACEHOLDER_VALUE, rendered)
            self.assertIn("redacted", rendered)
        self.assertIn(WEBHOOK, repr(credential))

    def test_an_unhandled_traceback_would_not_print_the_value(self):
        """A dataclass repr is how a secret escapes through an exception."""

        credential = SlackCredential(WEBHOOK, PLACEHOLDER_VALUE)
        buffer = io.StringIO()
        try:
            with redirect_stderr(buffer):
                raise RuntimeError(f"failing with {credential!r}")
        except RuntimeError as error:
            self.assertNotIn(PLACEHOLDER_VALUE, str(error))
        self.assertNotIn(PLACEHOLDER_VALUE, buffer.getvalue())


class TransientVersusPermanentTests(unittest.TestCase):
    """Which failures may destroy deliverable work, and which must preserve it.

    The permanent set is an allowlist, so the interesting assertion is the
    default: an unclassified code preserves the delivery on a bounded delay.
    Getting this wrong towards transient leaves work scheduled; getting it wrong
    towards permanent discards an alert, and only the second cannot be undone.
    """

    PERMANENT = {
        "container missing": "ResourceNotFoundException",
        "ssm container missing": "ParameterNotFound",
        "ssm version missing": "ParameterVersionNotFound",
        "denied": "AccessDeniedException",
        "denied short form": "AccessDenied",
        "kms denied": "KMSAccessDeniedException",
        "kms disabled": "KMSInvalidStateException",
        "validation": "ValidationException",
        "invalid parameter": "InvalidParameterException",
        "decryption failure": "DecryptionFailure",
    }
    TRANSIENT = {
        "throttling": "ThrottlingException",
        "throttling short form": "Throttling",
        "too many requests": "TooManyRequestsException",
        "request limit": "RequestLimitExceeded",
        "service unavailable": "ServiceUnavailable",
        "internal service error": "InternalServiceError",
        "internal server error": "InternalServerError",
        "internal failure": "InternalFailure",
        "request timeout": "RequestTimeout",
        "endpoint connection": "EndpointConnectionError",
        "connect timeout": "ConnectTimeoutError",
        "read timeout": "ReadTimeoutError",
        "expired session token": "ExpiredTokenException",
        "unrecognized or expired client credential": "UnrecognizedClientException",
        "unproven permanent signature failure": "InvalidSignatureException",
        "never reviewed": "SomeFutureCodeNobodyClassified",
        "empty code": "",
    }

    def test_permanent_codes_raise_credential_read_error(self):
        for label, code in self.PERMANENT.items():
            for build in (secrets_reader, ssm_reader):
                with self.subTest(label=label, store=build.__name__):
                    reader, _ = build(error=FakeClientError(code))
                    with self.assertRaises(CredentialReadError):
                        reader.read(SECRET_ID)

    def test_transient_codes_raise_credential_unavailable(self):
        for label, code in self.TRANSIENT.items():
            for build in (secrets_reader, ssm_reader):
                with self.subTest(label=label, store=build.__name__):
                    reader, _ = build(error=FakeClientError(code))
                    with self.assertRaises(CredentialUnavailable):
                        reader.read(SECRET_ID)

    def test_an_error_carrying_no_response_is_transient(self):
        """A botocore connection or timeout error has no response to classify."""

        for error in (
            RuntimeError("socket closed"),
            OSError("connection reset"),
            TimeoutError("read timed out"),
        ):
            with self.subTest(error=type(error).__name__):
                reader, _ = secrets_reader(error=error)
                with self.assertRaises(CredentialUnavailable) as caught:
                    reader.read(SECRET_ID)
                self.assertIn(UNKNOWN_ERROR_CODE, str(caught.exception))
                self.assertNotIn(str(error), str(caught.exception))

    def test_a_malformed_successful_response_is_permanent(self):
        """The provider answered, so the next read returns the same thing."""

        cases = (
            ("secrets non-mapping", *secrets_reader(response=["SecretString"])),
            ("secrets binary", *secrets_reader(response={"SecretBinary": b"\x00"})),
            ("secrets non-text", *secrets_reader(response={"SecretString": 12345})),
            ("ssm no parameter", *ssm_reader(response={})),
            ("ssm wrong type", *ssm_reader(response={"Parameter": {"Value": "v", "Type": "String"}})),
        )
        for label, reader, _ in cases:
            with self.subTest(label=label):
                with self.assertRaises(CredentialReadError):
                    reader.read(SECRET_ID)

    def test_the_transient_half_is_not_a_credential_read_error(self):
        """A shared base would route it straight back into the terminal path."""

        self.assertTrue(issubclass(CredentialUnavailable, CredentialError))
        self.assertFalse(issubclass(CredentialUnavailable, CredentialReadError))
        self.assertTrue(issubclass(CredentialReadError, CredentialError))


class ErrorCodeBoundTests(unittest.TestCase):
    """The code reaches a durable response class, so its shape is enforced."""

    def test_documented_codes_pass_through(self):
        for code in ("ThrottlingException", "AccessDeniedException", "ParameterNotFound", "A", "a1B2"):
            with self.subTest(code=code):
                self.assertEqual(_error_code(FakeClientError(code)), code)

    def test_a_code_at_the_bound_passes_and_one_past_it_does_not(self):
        at_bound = "a" * MAX_ERROR_CODE_CHARACTERS
        past_bound = "a" * (MAX_ERROR_CODE_CHARACTERS + 1)

        self.assertEqual(_error_code(FakeClientError(at_bound)), at_bound)
        self.assertEqual(_error_code(FakeClientError(past_bound)), UNKNOWN_ERROR_CODE)

    def test_unusable_code_shapes_become_unknown(self):
        cases = {
            "oversized": "E" * 5000,
            "punctuated": "Throttling.Exception",
            "spaced": "Throttling Exception",
            "newline": "Throttling\nInjected: 1",
            "empty": "",
            "non-ascii": "Thr\u00f6ttling",
            "underscored": "THROTTLING_EXCEPTION",
        }
        for label, code in cases.items():
            with self.subTest(label=label):
                self.assertEqual(_error_code(FakeClientError(code)), UNKNOWN_ERROR_CODE)

    def test_non_string_and_absent_codes_become_unknown(self):
        class Shapeless(Exception):
            def __init__(self, response):
                super().__init__("x")
                self.response = response

        shapes: tuple[Any, ...] = (
            None,
            {},
            {"Error": None},
            {"Error": {}},
            {"Error": {"Code": 500}},
            {"Error": {"Code": None}},
        )
        for response in shapes:
            with self.subTest(response=response):
                self.assertEqual(_error_code(Shapeless(response)), UNKNOWN_ERROR_CODE)
        self.assertEqual(_error_code(RuntimeError("no response attribute")), UNKNOWN_ERROR_CODE)

    def test_a_rejected_code_still_classifies_as_transient(self):
        """Rejecting the shape must not accidentally make a failure permanent."""

        reader, _ = secrets_reader(error=FakeClientError("E" * 5000))

        with self.assertRaises(CredentialUnavailable):
            reader.read(SECRET_ID)

    def test_no_provider_message_survives_the_bound(self):
        reader, _ = secrets_reader(error=FakeClientError("E" * 5000, PLACEHOLDER_VALUE))

        with self.assertRaises(CredentialError) as caught:
            reader.read(SECRET_ID)
        self.assertNotIn(PLACEHOLDER_VALUE, str(caught.exception))


class BotTokenValidationTests(unittest.TestCase):
    """The content check the configured kind cannot perform.

    Slack's current primary documentation gives bot tokens the `xoxb-` prefix.
    The segment count after it is not documented as stable, so it is deliberately
    not encoded: binding to an internal format would reject valid tokens the day
    Slack changes it.
    """

    def test_a_documented_bot_token_shape_is_accepted(self):
        for token in (
            BOT_TOKEN_VALUE,
            "-".join(("xoxb", "123456789012", "123456789012", "AbCdEfGhIjKlMnOpQrStUvWx")),
            "xoxb-" + "a" * (MAX_BOT_TOKEN_CHARACTERS - 5),
        ):
            with self.subTest(length=len(token)):
                self.assertEqual(validate_bot_token(token), token)

    def test_a_webhook_url_in_bot_token_storage_is_refused(self):
        """The case the configured kind passes and this one catches."""

        with self.assertRaises(InvalidBotToken):
            validate_bot_token(WEBHOOK_URL)

    def test_other_slack_token_classes_are_refused(self):
        for token in ("xoxp-123456789012-abcdef", "xapp-1-A0000-000-abcdef", "xoxa-2-abcdef123456"):
            with self.subTest(token=token[:6]):
                with self.assertRaises(InvalidBotToken):
                    validate_bot_token(token)

    def test_arbitrary_printable_values_are_refused(self):
        for token in ("just some text that is long enough", "AKIAIOSFODNN7EXAMPLE-not-a-slack-token"):
            with self.subTest(token=token[:12]):
                with self.assertRaises(InvalidBotToken):
                    validate_bot_token(token)

    def test_boundary_lengths_are_enforced(self):
        with self.assertRaises(InvalidBotToken):
            validate_bot_token("xoxb-short")
        with self.assertRaises(InvalidBotToken):
            validate_bot_token("xoxb-" + "a" * MAX_BOT_TOKEN_CHARACTERS)

    def test_unsafe_shapes_are_refused(self):
        cases = {
            "empty": "",
            "line break": "xoxb-1234567890-abc\nX-Injected: 1",
            "carriage return": "xoxb-1234567890-abc\rX",
            "tab": "xoxb-1234567890-abc\tdef",
            "space": "xoxb-1234567890 abcdefghij",
            "null": "xoxb-1234567890-abc\x00def",
            "non-ascii": "xoxb-1234567890-abc\u2019def",
            "leading whitespace": " xoxb-1234567890-abcdefghij",
        }
        for label, token in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(InvalidBotToken):
                    validate_bot_token(token)

    def test_a_non_string_is_refused(self):
        values: tuple[Any, ...] = (None, 12345, b"xoxb-1234567890-abcdef", ["xoxb-1234567890-abcdef"])
        for value in values:
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(InvalidBotToken):
                    validate_bot_token(value)

    def test_no_rejection_message_quotes_the_value(self):
        for token in (WEBHOOK_URL, "xoxp-123456789012-abcdef", "xoxb-1234567890-abc\nX", PLACEHOLDER_VALUE):
            with self.subTest(token=token[:16]):
                with self.assertRaises(InvalidBotToken) as caught:
                    validate_bot_token(token)
                rendered = f"{caught.exception}{caught.exception!r}"
                self.assertNotIn(token, rendered)
                # The prefix the message names is the expected one, never observed
                # content, so no fragment of the value can ride along.
                for fragment in (token[:12], token[-12:]):
                    if fragment and fragment != "xoxb-":
                        self.assertNotIn(fragment, rendered)


class MotoBackedTests(unittest.TestCase):
    """The adapters against the real AWS APIs, as moto implements them.

    The fake-client tests above pin the failure mapping precisely, which moto
    cannot express — it will not throttle on demand. These pin the success and
    refusal paths against the actual request and response shapes, so a wrong
    parameter name or a misread field fails here rather than at first deploy.
    """

    REGION = "us-east-1"

    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def test_secrets_manager_secret_string_round_trips(self):
        client = boto3.client("secretsmanager", region_name=self.REGION)
        client.create_secret(Name=SECRET_ID, SecretString=WEBHOOK_URL)
        reader = SecretsManagerCredentialReader(client, kind=WEBHOOK)

        credential = reader.read(SECRET_ID)

        self.assertEqual(credential.kind, WEBHOOK)
        self.assertEqual(credential.value, WEBHOOK_URL)

    def test_secrets_manager_missing_container_is_not_found(self):
        client = boto3.client("secretsmanager", region_name=self.REGION)
        reader = SecretsManagerCredentialReader(client, kind=WEBHOOK)

        with self.assertRaises(CredentialNotFound):
            reader.read("aws-public-change-alerting/slack/never-created")

    def test_secrets_manager_missing_value_is_empty(self):
        """A container created without a string value, as Terraform leaves it."""

        client = boto3.client("secretsmanager", region_name=self.REGION)
        client.create_secret(Name=SECRET_ID, SecretBinary=b"\x00\x01")
        reader = SecretsManagerCredentialReader(client, kind=WEBHOOK)

        with self.assertRaises(CredentialReadError):
            reader.read(SECRET_ID)

    def test_ssm_secure_string_round_trips_with_decryption(self):
        client = boto3.client("ssm", region_name=self.REGION)
        client.put_parameter(Name="/apcf/slack/bot", Value=BOT_TOKEN_VALUE, Type="SecureString")
        reader = SsmParameterCredentialReader(client, kind=BOT_TOKEN)

        credential = reader.read("/apcf/slack/bot")

        self.assertEqual(credential.kind, BOT_TOKEN)
        self.assertEqual(credential.value, BOT_TOKEN_VALUE)

    def test_ssm_plain_string_is_refused(self):
        client = boto3.client("ssm", region_name=self.REGION)
        client.put_parameter(Name="/apcf/slack/plain", Value=BOT_TOKEN_VALUE, Type="String")
        reader = SsmParameterCredentialReader(client, kind=BOT_TOKEN)

        with self.assertRaises(CredentialUnreadable) as caught:
            reader.read("/apcf/slack/plain")
        self.assertIn(SECURE_STRING, str(caught.exception))
        self.assertNotIn(BOT_TOKEN_VALUE, str(caught.exception))

    def test_ssm_string_list_is_refused(self):
        client = boto3.client("ssm", region_name=self.REGION)
        client.put_parameter(Name="/apcf/slack/list", Value=f"{BOT_TOKEN_VALUE},other", Type="StringList")
        reader = SsmParameterCredentialReader(client, kind=BOT_TOKEN)

        with self.assertRaises(CredentialUnreadable):
            reader.read("/apcf/slack/list")

    def test_ssm_missing_parameter_is_not_found(self):
        client = boto3.client("ssm", region_name=self.REGION)
        reader = SsmParameterCredentialReader(client, kind=BOT_TOKEN)

        with self.assertRaises(CredentialNotFound):
            reader.read("/apcf/slack/never-created")

    def test_the_stored_value_is_returned_whole(self):
        """No structured format: whatever bytes an operator stored come back."""

        client = boto3.client("ssm", region_name=self.REGION)
        stored = '{"url": "' + WEBHOOK_URL + '"}'
        client.put_parameter(Name="/apcf/slack/json", Value=stored, Type="SecureString")
        reader = SsmParameterCredentialReader(client, kind=WEBHOOK)

        # A JSON document is not unwrapped; it is simply the wrong credential and
        # the content validators refuse it later.
        self.assertEqual(reader.read("/apcf/slack/json").value, stored)


if __name__ == "__main__":
    unittest.main()
