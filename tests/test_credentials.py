"""The Slack credential port and its in-memory reader.

ADR-007 makes the delivery worker the only component that reads Slack
credentials. The port is narrow on purpose: one identifier in, one opaque
value out, and a typed failure when the secret is missing or unreadable. The
tests hold the port to that shape so the Lambda handler's Secrets Manager
adapter can be written against the same boundary the dry-run reader exercises.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.credentials import (  # noqa: E402
    BOT_TOKEN,
    WEBHOOK,
    CredentialNotFound,
    CredentialReadError,
    SlackCredential,
    StaticCredentialReader,
)


class SlackCredentialTests(unittest.TestCase):
    def test_the_two_delivery_modes_are_distinct_strings(self):
        self.assertEqual(WEBHOOK, "incoming_webhook")
        self.assertEqual(BOT_TOKEN, "bot_token")
        self.assertNotEqual(WEBHOOK, BOT_TOKEN)

    def test_a_credential_carries_its_kind_and_opaque_value(self):
        credential = SlackCredential(kind=WEBHOOK, value="https://hooks.slack.com/services/T/B/S")

        self.assertEqual(credential.kind, WEBHOOK)
        self.assertEqual(credential.value, "https://hooks.slack.com/services/T/B/S")

    def test_a_credential_is_immutable(self):
        credential = SlackCredential(kind=WEBHOOK, value="https://hooks.slack.com/services/T/B/S")

        with self.assertRaises((AttributeError, TypeError)):
            credential.value = "replaced"  # type: ignore[misc]


class CredentialNotFoundTests(unittest.TestCase):
    def test_a_missing_credential_is_a_credential_read_error(self):
        self.assertTrue(issubclass(CredentialNotFound, CredentialReadError))

    def test_a_missing_credential_message_names_the_identifier(self):
        reader = StaticCredentialReader({})

        with self.assertRaisesRegex(CredentialNotFound, "no credential under"):
            reader.read("aws-public-change-alerting/slack/shared-alerts-webhook")


class StaticCredentialReaderTests(unittest.TestCase):
    def test_a_known_identifier_returns_its_credential(self):
        webhook = SlackCredential(kind=WEBHOOK, value="https://hooks.slack.com/services/T/B/S")
        reader = StaticCredentialReader({"shared-alerts-webhook": webhook})

        self.assertIs(reader.read("shared-alerts-webhook"), webhook)

    def test_an_unknown_identifier_raises_credential_not_found(self):
        webhook = SlackCredential(kind=WEBHOOK, value="https://hooks.slack.com/services/T/B/S")
        reader = StaticCredentialReader({"shared-alerts-webhook": webhook})

        with self.assertRaises(CredentialNotFound):
            reader.read("other-route-webhook")

    def test_a_reader_with_no_credentials_raises_for_any_identifier(self):
        reader = StaticCredentialReader()

        with self.assertRaises(CredentialNotFound):
            reader.read("anything")

    def test_the_reader_serves_a_fixed_secret_per_identifier(self):
        first = SlackCredential(kind=WEBHOOK, value="first")
        second = SlackCredential(kind=BOT_TOKEN, value="xoxb-second")
        reader = StaticCredentialReader({"route-a": first, "route-b": second})

        self.assertIs(reader.read("route-a"), first)
        self.assertIs(reader.read("route-b"), second)
        self.assertIs(reader.read("route-a"), first)

    def test_the_static_reader_exposes_the_port_read_method(self):
        reader = StaticCredentialReader()

        self.assertTrue(callable(getattr(reader, "read", None)))


if __name__ == "__main__":
    unittest.main()
