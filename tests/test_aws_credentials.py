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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.aws_credentials import (  # noqa: E402
    MAX_CREDENTIAL_CHARACTERS,
    MAX_SECRET_ID_CHARACTERS,
    SECRETS_MANAGER,
    SSM_PARAMETER_STORE,
    SecretsManagerCredentialReader,
    SsmParameterCredentialReader,
    credential_reader_for,
)
from aws_public_change_feed.credentials import (  # noqa: E402
    BOT_TOKEN,
    WEBHOOK,
    CredentialAccessDenied,
    CredentialEmpty,
    CredentialNotFound,
    CredentialReadError,
    CredentialUnreadable,
    SlackCredential,
)

SECRET_ID = "aws-public-change-alerting/slack/shared-alerts-webhook"

# Deliberately not a real credential shape, and distinctive so a substring
# search for it in messages, reprs, and logs proves absence rather than luck.
PLACEHOLDER_VALUE = "PLACEHOLDER-NOT-A-REAL-CREDENTIAL-0123456789"


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
        for code in ("AccessDeniedException", "UnrecognizedClientException"):
            with self.subTest(code=code):
                reader, _ = secrets_reader(error=FakeClientError(code))
                with self.assertRaises(CredentialAccessDenied):
                    reader.read(SECRET_ID)

    def test_an_unrecognised_error_code_is_unreadable_and_bounded(self):
        reader, _ = secrets_reader(error=FakeClientError("ThrottlingException"))

        with self.assertRaises(CredentialUnreadable) as caught:
            reader.read(SECRET_ID)
        self.assertIn("ThrottlingException", str(caught.exception))

    def test_an_error_without_a_usable_code_still_maps(self):
        reader, _ = secrets_reader(error=RuntimeError("socket closed"))

        with self.assertRaises(CredentialUnreadable) as caught:
            reader.read(SECRET_ID)
        self.assertIn("Unknown", str(caught.exception))
        self.assertNotIn("socket closed", str(caught.exception))


class SsmParameterTests(unittest.TestCase):
    def test_a_secure_string_is_returned_and_decryption_is_requested(self):
        reader, client = ssm_reader(response={"Parameter": {"Value": PLACEHOLDER_VALUE, "Type": "SecureString"}})

        credential = reader.read(SECRET_ID)

        self.assertEqual(credential.kind, WEBHOOK)
        self.assertEqual(credential.value, PLACEHOLDER_VALUE)
        self.assertEqual(client.calls, [{"Name": SECRET_ID, "WithDecryption": True}])

    def test_decryption_is_always_requested(self):
        """Without it the call returns ciphertext, which would look like a value."""

        reader, client = ssm_reader(response={"Parameter": {"Value": PLACEHOLDER_VALUE}})

        reader.read(SECRET_ID)

        self.assertIs(client.calls[0]["WithDecryption"], True)

    def test_the_configured_kind_is_preserved(self):
        reader, _ = ssm_reader(response={"Parameter": {"Value": PLACEHOLDER_VALUE}}, kind=BOT_TOKEN)

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

    def test_a_response_without_a_parameter_is_unreadable(self):
        for response in ({}, {"Parameter": None}, {"Parameter": "value"}, ["Parameter"]):
            with self.subTest(response=response):
                reader, _ = ssm_reader(response=response)
                with self.assertRaises(CredentialUnreadable):
                    reader.read(SECRET_ID)

    def test_an_empty_parameter_value_is_empty(self):
        reader, _ = ssm_reader(response={"Parameter": {"Value": "  "}})

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

    def test_every_failure_is_a_credential_read_error(self):
        """The worker branches on the base class only, so all of them must be one."""

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
            ("provider error", *secrets_reader(error=FakeClientError("ThrottlingException", PLACEHOLDER_VALUE))),
            ("ssm non-text", *ssm_reader(response={"Parameter": {"Value": [PLACEHOLDER_VALUE]}})),
            ("ssm provider error", *ssm_reader(error=FakeClientError("InternalServerError", PLACEHOLDER_VALUE))),
        ]

    def test_no_raised_message_contains_the_value_or_the_identifier(self):
        for label, reader, _ in self.failures():
            with self.subTest(label=label):
                with self.assertRaises(CredentialReadError) as caught:
                    reader.read(SECRET_ID)
                rendered = f"{caught.exception}{caught.exception!r}"
                self.assertNotIn(PLACEHOLDER_VALUE, rendered)
                self.assertNotIn(SECRET_ID, rendered)

    def test_no_chained_cause_carries_the_provider_message(self):
        """`from None` is deliberate: a chained ClientError would print it."""

        reader, _ = secrets_reader(error=FakeClientError("ThrottlingException", PLACEHOLDER_VALUE))

        with self.assertRaises(CredentialReadError) as caught:
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
                with self.assertRaises(CredentialReadError):
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


if __name__ == "__main__":
    unittest.main()
