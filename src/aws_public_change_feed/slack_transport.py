"""The Slack HTTP transport: one request, reported as facts.

ADR-007 makes the delivery worker the only component that performs Slack HTTP
requests, and `worker.SlackSender` is the port it does that through. This module
is the production implementation for both modes chapter 04 defines.

The split with `worker.py` is the point of the design and is enforced by what
this module cannot express. It returns a `SlackResponse`, which carries a status
code, a bounded transport-failure class, a `bytes_sent` fact, a latency, a Slack
body error code, a `Retry-After` integer, and a message timestamp. It has no way
to name `posted`, `failed_retryable`, `failed_terminal`, or `delivery_unknown`,
so the ADR-004 mapping stays in `worker._classify` where one reader can check it
against the chapter. `Retry-After` is reported as the integer Slack sent, not
clamped, because `max_retry_after_seconds` is release policy and bounding it
here would apply a limit the release never saw.

`bytes_sent` is the fact the retry-safety decision rests on, and ADR-004 permits
an automatic retry only on proof that no request byte was sent. This adapter
claims `False` in exactly three places, each of which happens before a request
is written: a request that could not be constructed, a refused address set, and
a failure raised by an explicit `connect()` call. Everything after that
`connect()` returns claims `True`, including a timeout that may well have
occurred before the first byte, because "probably not sent" is not proof. The
explicit `connect()` is what makes the boundary observable: left to
`http.client`, the handshake would happen lazily inside `request()` and a
connect failure would be indistinguishable from a failure mid-write.

Nothing here logs. That is a deliberate absence rather than an omission —
chapter 05 excludes webhook URLs, tokens, authorization headers, response
bodies, complete payloads, and source titles and summaries from logs, and the
values this module handles are precisely those. A module with no logging
statements cannot leak through one, and the raised and returned values carry
only bounded classes and codes. Diagnosis happens through the worker, which
records a response class, a status, a latency, and `bytes_sent`.
"""

from __future__ import annotations

import http.client
import json
import re
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .credentials import WEBHOOK, SlackCredential
from .pinned_https import (
    ConnectionFactory,
    PinnedHTTPSConnection,
    Resolver,
    select_validated_address,
    system_resolver,
    verified_tls_context,
)
from .urls import FeedUrlRejected, UnsafeAddress, ValidatedUrl, validate_feed_url
from .worker import InvalidSlackWebhook, SlackDestination, SlackResponse, TransportError, validate_webhook_url

__all__ = [
    "MAX_SLACK_RESPONSE_BYTES",
    "SLACK_API_HOST",
    "SLACK_POST_MESSAGE_PATH",
    "SlackHttpSender",
]

# Chapter 04's bot-token controls: "the worker calls only the required Slack
# posting method". Both are constants rather than configuration so no release,
# candidate, or credential can redirect a bot-mode post.
SLACK_API_HOST = "slack.com"
SLACK_POST_MESSAGE_PATH = "/api/chat.postMessage"

# Slack's own responses are a few hundred bytes. The bound exists so a proxy
# error page or a redirected body cannot be buffered or parsed, not to
# accommodate a large legitimate answer.
MAX_SLACK_RESPONSE_BYTES = 64 * 1024

# A bot-mode error member is a documented snake_case code such as
# `channel_not_found`. Bounding its shape keeps an arbitrary string out of the
# stored response class, which an operator reads and which chapter 05 limits to
# bounded error codes.
_SLACK_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")

# A Slack message timestamp is `<seconds>.<microseconds>`. Bounded so an
# arbitrary string cannot reach the durable delivery record through `message_ts`.
_SLACK_TS = re.compile(r"\d{1,20}\.\d{1,20}")

# A bot token belongs in an Authorization header, so it must be a single line of
# printable ASCII. `credentials` already refuses line breaks; this refuses the
# rest of what would corrupt a header, before one is built.
_HEADER_SAFE_VALUE = re.compile(r"[\x20-\x7e]+")

_HTTP_OK = 200
_REDIRECT_RANGE = range(300, 400)


def _deterministic_body(payload: Mapping[str, Any]) -> bytes:
    """Serialize one request body the same way every time.

    Sorted keys and no incidental whitespace. Two workers rendering the same
    candidate therefore send identical bytes, which is what makes a captured
    request comparable in a test and a replay comparable in an incident.
    """

    return json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _retry_after_seconds(headers: Any) -> int | None:
    """`Retry-After` as an integer fact, or `None` when it is not one.

    Slack sends delay-seconds. HTTP also permits an HTTP-date, which is not an
    integer and is reported as absent rather than converted: the worker's
    contract is an integer count of seconds, and turning a date into one here
    would require this module to hold a clock and a skew opinion. A missing,
    non-numeric, or negative value is `None`, and the worker then falls back to
    its own bounded backoff.
    """

    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        return None
    text = str(raw).strip()
    if not text.isdigit():
        return None
    try:
        value = int(text)
    except ValueError:  # pragma: no cover - isdigit already proved the shape
        return None
    return value if value >= 0 else None


@dataclass
class SlackHttpSender:
    """A `SlackSender` that posts once over a DNS-pinned TLS connection.

    The resolver, TLS context, and connection factory are injected so a test can
    drive address selection, socket targeting, TLS hostname behaviour, request
    writes, response limits, and every failure boundary without contacting
    Slack. That is also why the failure mapping is expressed against
    `connection.connect()`, `connection.request()`, and
    `connection.getresponse()` rather than against a client library: those three
    calls are the boundary the truth table is written about.
    """

    resolver: Resolver = system_resolver
    max_response_bytes: int = MAX_SLACK_RESPONSE_BYTES
    ssl_context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context
    connection_factory: ConnectionFactory = field(default=PinnedHTTPSConnection)
    user_agent: str = "aws-public-change-alerting/0.1 (+slack delivery worker)"

    def post(
        self,
        payload: Mapping[str, Any],
        *,
        credential: SlackCredential,
        destination: SlackDestination,
        timeout_seconds: float,
    ) -> SlackResponse:
        try:
            target, body, headers = self._build_request(payload, credential, destination)
        except (InvalidSlackWebhook, FeedUrlRejected, ValueError):
            # No socket exists yet, so nothing was sent. The detail is dropped
            # rather than reported: in webhook mode it embeds the URL, and in
            # bot mode it would describe the token.
            return SlackResponse(error_class=TransportError.MALFORMED_URL, bytes_sent=False)

        started = time.monotonic()
        try:
            address = select_validated_address(target.hostname, target.port, self.resolver)
        except UnsafeAddress:
            # A resolved answer the anti-rebinding rules refuse. Terminal rather
            # than retryable: the name answered, and it answered with something
            # policy forbids.
            return SlackResponse(
                error_class=TransportError.MALFORMED_URL,
                bytes_sent=False,
                latency_ms=_elapsed_ms(started),
            )
        except OSError:
            # Resolution itself failed, which is transient and provably
            # pre-request.
            return SlackResponse(
                error_class=TransportError.CONNECT_FAILED,
                bytes_sent=False,
                latency_ms=_elapsed_ms(started),
            )

        context = verified_tls_context(self.ssl_context_factory)
        connection = self.connection_factory(target.hostname, address, target.port, timeout_seconds, context)
        try:
            return self._exchange(connection, target, body, headers, started, destination)
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - a close failure must not mask the outcome
                pass

    def _build_request(
        self,
        payload: Mapping[str, Any],
        credential: SlackCredential,
        destination: SlackDestination,
    ) -> tuple[ValidatedUrl, bytes, dict[str, str]]:
        """Resolve the mode into a target, a body, and headers.

        Raises rather than returning a failure, so `post` has one place that
        turns an unconstructable request into `MALFORMED_URL`.
        """

        if destination.mode == WEBHOOK:
            # Chapter 04 requires this at the network boundary even though
            # `process_delivery` already applied it to the same value. The
            # credential is mutable deployment state read on every attempt, and
            # this is the check standing immediately before the socket, so it is
            # the one that cannot be skipped by a future caller.
            target = validate_webhook_url(credential.value, approved_hosts=destination.approved_webhook_hosts)
            body = _deterministic_body(payload)
            headers = self._headers(target, len(body))
            return target, body, headers

        token = credential.value
        if _HEADER_SAFE_VALUE.fullmatch(token) is None:
            raise ValueError("bot token is not usable as a header value")
        target = validate_feed_url(f"https://{SLACK_API_HOST}{SLACK_POST_MESSAGE_PATH}", [SLACK_API_HOST])
        # `channel` is written after the payload, so a rendered payload that
        # carries one cannot choose the destination. The release route is the
        # only authority for where a bot-mode message goes.
        body = _deterministic_body({**dict(payload), "channel": destination.channel_id})
        headers = self._headers(target, len(body))
        headers["Authorization"] = f"Bearer {token}"
        return target, body, headers

    def _headers(self, target: ValidatedUrl, content_length: int) -> dict[str, str]:
        return {
            # The connection host is the pinned IP literal, so Host is explicit.
            "Host": target.hostname,
            "User-Agent": self.user_agent,
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(content_length),
            "Accept": "application/json",
            # Compression is not advertised, so a compressed body is a protocol
            # violation rather than something to inflate inside a limit.
            "Accept-Encoding": "identity",
            "Connection": "close",
        }

    def _exchange(
        self,
        connection: http.client.HTTPSConnection,
        target: ValidatedUrl,
        body: bytes,
        headers: Mapping[str, str],
        started: float,
        destination: SlackDestination,
    ) -> SlackResponse:
        # Connect explicitly. This is what makes `bytes_sent=False` provable:
        # the socket and the handshake complete here, so a failure below this
        # point is a failure with a live connection and a request in flight.
        try:
            connection.connect()
        except ssl.SSLError:
            return SlackResponse(
                error_class=TransportError.TLS_FAILED, bytes_sent=False, latency_ms=_elapsed_ms(started)
            )
        except (TimeoutError, OSError):
            return SlackResponse(
                error_class=TransportError.CONNECT_FAILED, bytes_sent=False, latency_ms=_elapsed_ms(started)
            )

        # Past this line every failure reports `bytes_sent=True`. A write may
        # have reached Slack, and ADR-004 requires proof rather than likelihood.
        try:
            connection.request("POST", target.path_with_query, body=body, headers=dict(headers))
            response = connection.getresponse()
        except TimeoutError:
            return SlackResponse(error_class=TransportError.TIMEOUT, bytes_sent=True, latency_ms=_elapsed_ms(started))
        except (ConnectionError, http.client.HTTPException):
            return SlackResponse(
                error_class=TransportError.CONNECTION_LOST, bytes_sent=True, latency_ms=_elapsed_ms(started)
            )
        except (ssl.SSLError, OSError):
            return SlackResponse(
                error_class=TransportError.READ_FAILED, bytes_sent=True, latency_ms=_elapsed_ms(started)
            )

        return self._read_response(response, started, destination)

    def _read_response(self, response: Any, started: float, destination: SlackDestination) -> SlackResponse:
        status = int(response.status)
        retry_after = _retry_after_seconds(response.headers)

        if status in _REDIRECT_RANGE:
            # Refused by not following it. The status is still the fact, and the
            # worker makes a redirect terminal; a followed redirect would reach
            # a host that passed neither the allowlist nor address validation.
            return SlackResponse(
                status_code=status,
                bytes_sent=True,
                latency_ms=_elapsed_ms(started),
                retry_after_seconds=retry_after,
            )

        try:
            # One byte past the limit, so a body that exactly fills it is read
            # and anything larger is refused without buffering all of it.
            raw = response.read(self.max_response_bytes + 1)
        except TimeoutError:
            return SlackResponse(error_class=TransportError.TIMEOUT, bytes_sent=True, latency_ms=_elapsed_ms(started))
        except (ConnectionError, http.client.HTTPException):
            return SlackResponse(
                error_class=TransportError.CONNECTION_LOST, bytes_sent=True, latency_ms=_elapsed_ms(started)
            )
        except (ssl.SSLError, OSError):
            return SlackResponse(
                error_class=TransportError.READ_FAILED, bytes_sent=True, latency_ms=_elapsed_ms(started)
            )

        if len(raw) > self.max_response_bytes:
            # A status arrived, so the request reached Slack, but the answer is
            # unusable. Reporting the status alone would classify an unread
            # `ok:false` as success, so this is reported as a read failure with
            # bytes sent, which the worker makes `delivery_unknown` for review.
            return SlackResponse(
                error_class=TransportError.READ_FAILED, bytes_sent=True, latency_ms=_elapsed_ms(started)
            )

        if status != _HTTP_OK or destination.mode == WEBHOOK:
            # A webhook answers `ok` as plain text, so the status is the whole
            # outcome. Any non-200 is likewise decided by its status, and its
            # body is neither needed nor parsed.
            return SlackResponse(
                status_code=status,
                bytes_sent=True,
                latency_ms=_elapsed_ms(started),
                retry_after_seconds=retry_after,
            )

        return self._bot_outcome(raw, status, retry_after, started)

    def _bot_outcome(self, raw: bytes, status: int, retry_after: int | None, started: float) -> SlackResponse:
        """Read `ok`, `error`, and `ts` out of a bot-mode 200 body.

        The Web API answers HTTP 200 with `ok: false` for token, channel, and
        payload faults, so a 200 alone does not mean posted. A body that cannot
        be read as that documented shape is reported as a read failure with
        bytes sent — `delivery_unknown` — rather than as success, because the
        message may well have been delivered.
        """

        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return SlackResponse(
                error_class=TransportError.READ_FAILED, bytes_sent=True, latency_ms=_elapsed_ms(started)
            )
        if not isinstance(document, dict) or not isinstance(document.get("ok"), bool):
            return SlackResponse(
                error_class=TransportError.READ_FAILED, bytes_sent=True, latency_ms=_elapsed_ms(started)
            )

        if document["ok"]:
            raw_ts = document.get("ts")
            message_ts = raw_ts if isinstance(raw_ts, str) and _SLACK_TS.fullmatch(raw_ts) else None
            return SlackResponse(
                status_code=status,
                bytes_sent=True,
                latency_ms=_elapsed_ms(started),
                retry_after_seconds=retry_after,
                message_ts=message_ts,
            )

        raw_error = document.get("error")
        # An unbounded or absent error member becomes a fixed code rather than
        # reaching the stored response class verbatim.
        code = raw_error if isinstance(raw_error, str) and _SLACK_ERROR_CODE.fullmatch(raw_error) else "unknown_error"
        return SlackResponse(
            status_code=status,
            bytes_sent=True,
            latency_ms=_elapsed_ms(started),
            slack_error=code,
            retry_after_seconds=retry_after,
        )


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since `started`, never negative.

    `SlackResponse` requires a non-negative integer, and a monotonic clock can
    report the same tick twice on a fast local exchange.
    """

    return max(0, int((time.monotonic() - started) * 1000))
