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

Failures split on one question the worker does branch on: can another identical
read succeed without someone changing something?

`CredentialReadError` is the permanent half. An absent identifier, a denied
grant, a missing container, an empty value, a binary secret, an SSM parameter
that is not a `SecureString`, and a well-formed response whose shape is unusable
all describe a configuration state, so the worker records `failed_terminal` and
an operator fixes the container. A malformed successful response belongs here
rather than with the transient half precisely because the provider answered: the
next read returns the same unusable representation.

`CredentialUnavailable` is the transient half — throttling, a service outage, an
internal provider failure, an endpoint connection failure, a read timeout. The
credential may be perfectly correct and simply unreadable for a moment. Resolving
that terminally destroys deliverable work over a provider hiccup, so the worker
records `failed_retryable` with a bounded delay and no Slack call.

It is deliberately not a subclass of `CredentialReadError`. A shared base would
make `except CredentialReadError` — which every existing caller writes — silently
swallow the transient case back into the terminal path, which is the defect this
split exists to fix. The two are siblings under `CredentialError` so a caller that
genuinely wants either can still say so.

An unrecognised provider error code is transient. The permanent set is an
explicit reviewed allowlist, so a code nobody has classified preserves the work
instead of discarding it, and the cost of being wrong is a bounded retry rather
than a lost alert.

Within each half the subclasses are named after the condition an operator has to
fix — absent, denied, empty, unreadable, unavailable — rather than after an SDK
exception, because the worker reads the half and the operator reads the name.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

__all__ = [
    "BOT_TOKEN",
    "CredentialAccessDenied",
    "CredentialEmpty",
    "CredentialError",
    "CredentialNotFound",
    "CredentialReadError",
    "CredentialReader",
    "CredentialUnavailable",
    "CredentialUnreadable",
    "InvalidBotToken",
    "MAX_BOT_TOKEN_CHARACTERS",
    "MIN_BOT_TOKEN_CHARACTERS",
    "SlackCredential",
    "StaticCredentialReader",
    "WEBHOOK",
    "validate_bot_token",
]

# The two delivery modes chapter 04 names. The kind tells the Slack client how
# to use the value and which runtime validation applies to it.
WEBHOOK = "incoming_webhook"
BOT_TOKEN = "bot_token"


class CredentialError(Exception):
    """Any credential read failure, permanent or transient.

    Exists so a caller that genuinely wants both halves can catch one type. The
    worker does not: it catches the two halves separately, because the whole
    point of the split is that they resolve to different delivery states.
    """


class CredentialReadError(CredentialError):
    """A credential cannot be read until someone changes something.

    Raising here rather than leaking the storage adapter's own exception keeps
    the worker's classification stable: a permanently unreadable secret is a
    configuration correction, so the worker records `failed_terminal` and an
    operator fixes the secret container and replays.
    """


class CredentialUnavailable(CredentialError):
    """The credential store could not answer, but nothing proves it is wrong.

    Throttling, an outage, an internal provider failure, a connection failure, a
    read timeout. The worker records `failed_retryable` with a bounded delay,
    makes no Slack call, and leaves the network-attempt budget untouched, because
    no Slack attempt happened.

    Not a `CredentialReadError` on purpose. Every caller written before this split
    catches `CredentialReadError`, so subclassing would route the transient case
    straight back into the terminal path it was introduced to escape.
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


class InvalidBotToken(ValueError):
    """A stored value cannot be a Slack bot token.

    Separate from the credential-read errors because the read succeeded. The
    container held something; it is the content that is wrong, and no retry of
    the read changes it.
    """


# Slack's current primary documentation gives bot tokens the `xoxb-` prefix, and
# that prefix is the whole check. The number and shape of the segments after it
# are not documented as stable, so encoding a segment count would bind this
# repository to an internal format that can change without notice and would
# reject valid tokens when it does.
_BOT_TOKEN_PREFIX = "xoxb-"

# Wide enough for any documented token and every rotation format Slack has
# shipped, narrow enough that a pasted file or certificate is refused before it
# becomes a header.
MIN_BOT_TOKEN_CHARACTERS = 16
MAX_BOT_TOKEN_CHARACTERS = 512

# One line of printable ASCII. A bot token is placed in an HTTP header, so a
# control character or a non-ASCII byte would corrupt the request rather than
# merely fail authentication.
_BOT_TOKEN_SHAPE = re.compile(r"[\x21-\x7e]+")


def validate_bot_token(value: str) -> str:
    """Return the value if it can be a Slack bot token, or raise.

    One implementation, called twice on purpose: `process_delivery` runs it
    before the network-attempt counter moves, so a wrong stored value is
    `failed_terminal` without spending an attempt Slack never saw, and the
    transport runs it again immediately before socket work, because that is the
    check standing between a credential and a real request.

    The configured kind cannot do this job. It records which mode the release
    declares, so it detects a release-versus-deployment mode mismatch — a bot
    container consulted while the release says webhook. It says nothing about
    what an operator actually stored, so a webhook URL pasted into the bot-token
    container passes the kind check and fails here.

    Raises `InvalidBotToken` with no part of the value in the message.
    """

    if not isinstance(value, str):
        raise InvalidBotToken("bot token is not text")
    if not value.startswith(_BOT_TOKEN_PREFIX):
        # Names the expected prefix, never the observed one: the observed value
        # is the secret, and a webhook URL stored here would otherwise be echoed.
        raise InvalidBotToken(f"bot token must begin with {_BOT_TOKEN_PREFIX!r}")
    if not MIN_BOT_TOKEN_CHARACTERS <= len(value) <= MAX_BOT_TOKEN_CHARACTERS:
        raise InvalidBotToken(
            f"bot token length must be between {MIN_BOT_TOKEN_CHARACTERS} and {MAX_BOT_TOKEN_CHARACTERS} characters"
        )
    if _BOT_TOKEN_SHAPE.fullmatch(value) is None:
        raise InvalidBotToken("bot token must be one line of printable ASCII")
    return value


class CredentialReader(Protocol):
    """The narrow secrets surface the worker needs.

    `secret_id` is the exact identifier the inventory names — a route's
    `credential_secret_id` in webhook mode or the deployment-wide
    `bot_token_secret_id` in bot mode. A failed read raises `CredentialError`;
    callers that decide a delivery outcome distinguish permanent
    `CredentialReadError` from transient `CredentialUnavailable`.
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
