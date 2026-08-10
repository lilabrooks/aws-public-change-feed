"""Slack route credentials behind a narrow port.

ADR-007 and chapter 05 make the delivery worker the only component that reads
Slack credentials or performs Slack HTTP requests. The worker core never puts a
secret value in a log, a delivery record, a candidate, or a fixture: it asks
`CredentialReader` for the route's credential by the exact identifier the
inventory names and hands the opaque value to the Slack client for one call.

The real Secrets Manager adapter belongs with the Lambda handler, which is where
boto3 wiring lives. This module defines the port and an in-memory reader the
tests and dry runs use, so the worker's decisions are exercised against the same
boundary that runs in production without a deployed secret.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "CredentialReadError",
    "CredentialReader",
    "SlackCredential",
    "StaticCredentialReader",
]

# The two delivery modes chapter 04 names. The kind tells the Slack client how
# to use the value and which runtime validation applies to it.
WEBHOOK = "incoming_webhook"
BOT_TOKEN = "bot_token"


class CredentialReadError(Exception):
    """A credential could not be read.

    Raising here rather than leaking the storage adapter's own exception keeps
    the worker's classification stable: a missing or unreadable secret is a
    configuration correction, so the worker records `failed_terminal` and an
    operator fixes the secret container and replays.
    """


class CredentialNotFound(CredentialReadError):
    """No credential exists under the identifier the inventory names."""


@dataclass(frozen=True, slots=True)
class SlackCredential:
    """The opaque secret value for one Slack route.

    Never logged, serialized into a delivery record, or written to a fixture.
    `kind` is `WEBHOOK` when `value` is an incoming-webhook URL that must pass
    the chapter 04 webhook controls, and `BOT_TOKEN` when `value` is a bot token
    used against the fixed Slack API host.
    """

    kind: str
    value: str


class CredentialReader(Protocol):
    """The narrow secrets surface the worker needs.

    `secret_id` is the exact identifier the inventory names — a route's
    `credential_secret_id` in webhook mode or the deployment-wide
    `bot_token_secret_id` in bot mode. A failed read raises `CredentialReadError`.
    """

    def read(self, secret_id: str) -> SlackCredential: ...


class StaticCredentialReader:
    """A reader serving a fixed secret per identifier, for tests and dry runs."""

    def __init__(self, credentials: Mapping[str, SlackCredential] | None = None) -> None:
        self._credentials = dict(credentials or {})

    def read(self, secret_id: str) -> SlackCredential:
        try:
            return self._credentials[secret_id]
        except KeyError:
            raise CredentialNotFound(f"no credential under {secret_id}") from None
