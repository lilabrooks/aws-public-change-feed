"""Safe conditional fetching of public feeds.

Chapter 04 requires the watcher to resolve and validate addresses, connect to
one validated address while preserving the approved hostname for TLS SNI and
certificate verification, send conditional headers, and reject redirects,
unsupported content types, oversized responses, and slow responses. The HTTP
client must not silently re-resolve to an unchecked address.

The standard library is used deliberately. Connecting to a pinned address while
verifying the certificate against the configured hostname needs control over
socket creation and the TLS handshake, and every layer that hides those makes
the pinning harder to prove.
"""

from __future__ import annotations

import http.client
import socket
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .urls import ValidatedUrl, validate_addresses

__all__ = [
    "ACCEPTED_CONTENT_TYPES",
    "FeedFetching",
    "FetchOutcome",
    "FetchRejected",
    "FeedFetcher",
    "MAX_RESPONSE_BYTES",
    "Resolver",
    "system_resolver",
]

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 15.0

ACCEPTED_CONTENT_TYPES = frozenset(
    {
        "application/atom+xml",
        "application/rss+xml",
        "application/xml",
        "text/xml",
    }
)

Resolver = Callable[[str, int], Sequence[str]]


class FetchRejected(Exception):
    """A fetch failed policy. Carries a bounded class for metrics and logs."""

    def __init__(self, reason_class: str, detail: str) -> None:
        super().__init__(f"{reason_class}: {detail}")
        self.reason_class = reason_class
        self.detail = detail


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """The result of one feed fetch."""

    status: int
    body: bytes = b""
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    address: str = ""

    @property
    def not_modified(self) -> bool:
        return self.status == 304


class FeedFetching(Protocol):
    """What the watcher needs from a fetcher, so doubles are substitutable."""

    def fetch(
        self,
        target: ValidatedUrl,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchOutcome: ...


def system_resolver(hostname: str, port: int) -> Sequence[str]:
    """Resolve every A and AAAA record for the hostname."""

    infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    ordered: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in ordered:
            ordered.append(address)
    return ordered


class _PinnedConnection(http.client.HTTPSConnection):
    """Connect to a validated address, verify the certificate against the name.

    ``http.client`` would use the connection host for SNI, which would be the
    pinned IP literal. Overriding ``connect`` keeps the approved hostname in
    both the handshake and the certificate check while the socket goes to the
    address DNS validation already approved.
    """

    def __init__(self, hostname: str, address: str, port: int, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(address, port=port, timeout=timeout, context=context)
        self._sni_hostname = hostname
        self._tls_context = context

    def connect(self) -> None:
        raw_socket = socket.create_connection((self.host, self.port), self.timeout)
        try:
            self.sock = self._tls_context.wrap_socket(raw_socket, server_hostname=self._sni_hostname)
        except Exception:
            raw_socket.close()
            raise


@dataclass
class FeedFetcher:
    """Fetches one feed under the chapter 04 network policy."""

    resolver: Resolver = system_resolver
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = MAX_RESPONSE_BYTES
    ssl_context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context
    connection_factory: Callable[[str, str, int, float, ssl.SSLContext], http.client.HTTPSConnection] = field(
        default=_PinnedConnection
    )

    def fetch(
        self,
        target: ValidatedUrl,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchOutcome:
        address = self._select_address(target)
        context = self.ssl_context_factory()
        # Both are default-on; asserting them keeps a misconfigured context
        # from silently disabling verification.
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED

        connection = self.connection_factory(target.hostname, address, target.port, self.timeout_seconds, context)
        try:
            response = self._request(connection, target, address, etag, last_modified)
        finally:
            connection.close()
        return response

    def _select_address(self, target: ValidatedUrl) -> str:
        try:
            candidates = self.resolver(target.hostname, target.port)
        except OSError as error:
            raise FetchRejected("dns", f"{target.hostname}: {error}") from error
        try:
            validated = validate_addresses(list(candidates))
        except ValueError as error:
            raise FetchRejected("dns", str(error)) from error
        return validated[0]

    def _headers(self, target: ValidatedUrl, etag: str | None, last_modified: str | None) -> dict[str, str]:
        headers = {
            # The connection host is an IP literal, so Host is set explicitly.
            "Host": target.hostname,
            "User-Agent": "aws-public-change-alerting/0.1 (+public feed watcher)",
            "Accept": ", ".join(sorted(ACCEPTED_CONTENT_TYPES)),
            # Compression is not advertised, so a compressed body is a policy
            # violation rather than something to decompress within a limit.
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        return headers

    def _request(
        self,
        connection: http.client.HTTPSConnection,
        target: ValidatedUrl,
        address: str,
        etag: str | None,
        last_modified: str | None,
    ) -> FetchOutcome:
        headers = self._headers(target, etag, last_modified)
        try:
            connection.request("GET", target.path_with_query, headers=headers)
            response = connection.getresponse()
        except ssl.SSLError as error:
            raise FetchRejected("tls", f"{target.hostname}: {error}") from error
        except TimeoutError as error:
            raise FetchRejected("timeout", f"{target.hostname}: {error}") from error
        except OSError as error:
            raise FetchRejected("network", f"{target.hostname}: {error}") from error

        status = response.status
        # 304 is checked first: it shares the 3xx range but is the successful
        # answer to a conditional request, not a redirect.
        if status == 304:
            return FetchOutcome(status=304, address=address)
        if 300 <= status < 400:
            # Following a redirect would reach a destination that never passed
            # host allowlisting or address validation.
            raise FetchRejected("redirect", f"{target.hostname}: status {status}")
        if status != 200:
            raise FetchRejected("status", f"{target.hostname}: status {status}")

        self._reject_unsupported_body(target, response.headers)
        body = self._read_bounded(target, response)
        return FetchOutcome(
            status=200,
            body=body,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            content_type=response.headers.get("Content-Type"),
            address=address,
        )

    def _reject_unsupported_body(self, target: ValidatedUrl, headers: Any) -> None:
        encoding = (headers.get("Content-Encoding") or "identity").strip().casefold()
        if encoding != "identity":
            raise FetchRejected("content_encoding", f"{target.hostname}: {encoding}")

        raw_type = headers.get("Content-Type") or ""
        media_type = raw_type.split(";", 1)[0].strip().casefold()
        if media_type not in ACCEPTED_CONTENT_TYPES:
            raise FetchRejected("content_type", f"{target.hostname}: {media_type or '<missing>'}")

        declared = headers.get("Content-Length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError as error:
                raise FetchRejected("response_limit", f"{target.hostname}: malformed Content-Length") from error
            if length > self.max_response_bytes:
                raise FetchRejected("response_limit", f"{target.hostname}: declared {length} bytes")

    def _read_bounded(self, target: ValidatedUrl, response: http.client.HTTPResponse) -> bytes:
        # Read one byte past the limit so a body that exactly fills it is
        # accepted while anything larger is refused without buffering it all.
        try:
            body = response.read(self.max_response_bytes + 1)
        except TimeoutError as error:
            raise FetchRejected("timeout", f"{target.hostname}: {error}") from error
        except OSError as error:
            raise FetchRejected("network", f"{target.hostname}: {error}") from error
        if len(body) > self.max_response_bytes:
            raise FetchRejected("response_limit", f"{target.hostname}: body exceeds {self.max_response_bytes} bytes")
        return body
