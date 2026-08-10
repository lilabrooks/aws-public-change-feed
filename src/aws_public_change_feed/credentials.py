"""Slack route credentials behind a narrow port.

ADR-007 and chapter 05 make the delivery worker the only component that reads
Slack credentials or performs Slack HTTP requests. The worker core never puts a
secret value in a log, a delivery record, a candidate, or a fixture: it asks
`CredentialReader` for the route's credential by the exact identifier the
inventory names and hands the opaque value to the Slack client for one call.

This module defines the port, its error taxonomy, and an in-memory reader the
tests and dry runs use, so the worker's decisions are exercised against the same
boundary that runs in production without a deployed secret. The AWS-backed
readers live in `aws_credentials`, one per `secret_store` value the deployment
contract accepts; which one a deployment uses is a composition-root choice, so
neither the port nor the worker names a store.

Every failure is a `CredentialReadError` subclass. The worker maps the base
class to `failed_terminal` and does not branch on the subclass, so the
distinctions exist for operators reading a bounded reason rather than for
control flow. That is why they are named after the condition an operator has to
fix — absent, denied, empty, unreadable — rather than after an SDK exception.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

__all__ = [
    "BOT_TOKEN",
    "CredentialAccessDenied",
    "CredentialEmpty",
    "CredentialNotFound",
    "CredentialReadError",
    "CredentialReader",
    "CredentialUnreadable",
    "SlackCredential",
    "StaticCredentialReader",
    "WEBHOOK",
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


class CredentialAccessDenied(CredentialReadError):
    """The runtime role may not read the identifier the inventory names.

    Distinct from `CredentialNotFound` because the operator fix differs: a
    denial is an IAM or KMS grant, an absence is a secret that was never
    created. Chapter 05 gives the worker read access to configured Slack
    secrets only, so a denial usually means the identifier drifted from the
    grant rather than that the grant is wrong.
    """


class CredentialEmpty(CredentialReadError):
    """The identifier exists but holds no usable credential text.

    A container created by Terraform before an operator populated it reads
    back present and empty. Treating that as a distinct condition keeps it from
    being reported as a malformed credential, which would send the operator
    looking at the value instead of at whether one was ever written.
    """


class CredentialUnreadable(CredentialReadError):
    """The response could not be turned into a credential value.

    Covers a binary-only secret, a response missing the field the API
    documents, and any other SDK failure the adapter cannot attribute to
    absence, denial, or emptiness. The provider's message is deliberately not
    carried forward: chapter 05 excludes provider messages and response bodies
    from durable errors and logs, and this is the path most likely to quote a
    value back.
    """


@dataclass(frozen=True, slots=True)
class SlackCredential:
    """The opaque secret value for one Slack route.

    Never logged, serialized into a delivery record, or written to a fixture.
    `kind` is `WEBHOOK` when `value` is an incoming-webhook URL that must pass
    the chapter 04 webhook controls, and `BOT_TOKEN` when `value` is a bot token
    used against the fixed Slack API host.

    `value` is excluded from `repr`. Chapter 05 keeps webhook URLs and tokens
    out of logs, and a dataclass's generated `repr` is the most likely way one
    escapes: it is what an f-string, a `logging` call with the object as an
    argument, an exception rendering its own arguments, and an assertion
    failure all reach for. Excluding it here is a property of the type rather
    than a rule every call site has to remember.
    """

    kind: str
    value: str = field(repr=False)

    def __repr__(self) -> str:
        # Length rather than a prefix. A prefix of a webhook URL is still the
        # approved host and the `/services/` path, and a prefix of a bot token
        # is its `xoxb-` class; neither is needed to diagnose a mix-up, and the
        # kind already says which one this is meant to be.
        return f"SlackCredential(kind={self.kind!r}, value=<redacted {len(self.value)} characters>)"


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
