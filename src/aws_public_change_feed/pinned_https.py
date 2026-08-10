"""DNS-validated, address-pinned HTTPS connections.

Chapter 04 requires the watcher to resolve every A and AAAA record, reject any
answer that is not a global address, and connect to one validated address while
preserving the approved hostname for TLS SNI and certificate verification. Its
incoming-webhook controls then require Slack delivery to "resolve and connect
using the same anti-rebinding controls as feed acquisition".

"The same controls" is a claim about one implementation, so this module is that
implementation and both callers import it. A second copy would satisfy the
sentence on the day it was written and drift afterwards, which is the failure
this repository keeps finding: two checkers that agree until one is edited.

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

from .urls import validate_addresses

__all__ = [
    "ConnectionFactory",
    "PinnedHTTPSConnection",
    "Resolver",
    "select_validated_address",
    "system_resolver",
    "verified_tls_context",
]

Resolver = Callable[[str, int], Sequence[str]]
ConnectionFactory = Callable[[str, str, int, float, ssl.SSLContext], http.client.HTTPSConnection]


def system_resolver(hostname: str, port: int) -> Sequence[str]:
    """Resolve every A and AAAA record for the hostname."""

    infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    ordered: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in ordered:
            ordered.append(address)
    return ordered


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a validated address, verify the certificate against the name.

    ``http.client`` would use the connection host for SNI, which would be the
    pinned IP literal. Overriding ``connect`` keeps the approved hostname in
    both the handshake and the certificate check while the socket goes to the
    address DNS validation already approved.

    Because the socket target is an IP literal, no name is resolved during
    connection setup, so the address checked is the address used. Callers set
    the ``Host`` header explicitly for the same reason.
    """

    def __init__(self, hostname: str, address: str, port: int, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(address, port=port, timeout=timeout, context=context)
        self._sni_hostname = hostname
        self._tls_context = context

    @property
    def sni_hostname(self) -> str:
        """The name used for SNI and certificate verification."""

        return self._sni_hostname

    def connect(self) -> None:
        raw_socket = socket.create_connection((self.host, self.port), self.timeout)
        try:
            self.sock = self._tls_context.wrap_socket(raw_socket, server_hostname=self._sni_hostname)
        except Exception:
            raw_socket.close()
            raise


def select_validated_address(hostname: str, port: int, resolver: Resolver) -> str:
    """Resolve the hostname and return one address every record justified.

    Raises `OSError` when resolution itself fails and `UnsafeAddress` when any
    resolved address is not a public destination. The two are deliberately
    distinguishable: a resolver outage is transient, while a private answer is
    a policy refusal, and the callers map them to different outcomes.

    Every address is checked, not merely the one returned, so a name answering
    with one public and one private address is refused outright.
    """

    candidates = resolver(hostname, port)
    validated = validate_addresses(list(candidates))
    return validated[0]


def verified_tls_context(factory: Callable[[], ssl.SSLContext] = ssl.create_default_context) -> ssl.SSLContext:
    """Build a TLS context with hostname and certificate verification asserted.

    Both are default-on. Setting them explicitly keeps a misconfigured or
    substituted factory from silently disabling verification on a connection
    whose whole security argument is that the certificate is checked against
    the approved name rather than the pinned address.
    """

    context = factory()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context
