"""`PinnedHTTPSConnection` itself, not a fake standing in for it.

Every committed transport test replaces this class, which is reasonable — they
are about the adapter's decisions — but it left the one claim the class exists to
make untested: that the socket goes to the address DNS validation approved while
the certificate is verified against the approved hostname. A fake cannot show
that, because a fake is where the behaviour would be missing.

So these tests patch `socket.create_connection` and supply a recording TLS
context, and then assert on the arguments the real `connect()` passes. No Slack
call and no network access: the patched creator returns a stub socket and the
recording context returns a stub wrapped socket.

The last test is the anti-rebinding claim stated negatively. Passing an IP
literal as the connection host is what stops a second name resolution during
connection setup, so the test asserts both that the host is a literal and that no
resolver function is reached.
"""

import ipaddress
import socket
import ssl
import sys
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.pinned_https import (  # noqa: E402
    PinnedHTTPSConnection,
    select_validated_address,
    system_resolver,
    verified_tls_context,
)
from aws_public_change_feed.urls import UnsafeAddress  # noqa: E402

HOSTNAME = "hooks.slack.com"
ADDRESS = "93.184.216.34"
ADDRESS_V6 = "2606:4700::1111"
PORT = 443


class StubSocket:
    def __init__(self, label="raw"):
        self.label = label
        self.closed = False

    def close(self):
        self.closed = True


class RecordingContext:
    """Stands in for an `ssl.SSLContext`, recording the handshake arguments."""

    def __init__(self, wrap_error=None):
        self.check_hostname = True
        self.verify_mode = ssl.CERT_REQUIRED
        self.calls = []
        self._wrap_error = wrap_error
        self.wrapped = StubSocket("wrapped")

    def wrap_socket(self, raw_socket, *, server_hostname=None, **kwargs):
        self.calls.append({"socket": raw_socket, "server_hostname": server_hostname, "extra": kwargs})
        if self._wrap_error is not None:
            raise self._wrap_error
        return self.wrapped


class ConnectTests(unittest.TestCase):
    def connect(self, *, hostname=HOSTNAME, address=ADDRESS, wrap_error=None, timeout=9.5):
        context = RecordingContext(wrap_error=wrap_error)
        connection = PinnedHTTPSConnection(hostname, address, PORT, timeout, cast(ssl.SSLContext, context))
        raw = StubSocket()
        with mock.patch.object(socket, "create_connection", return_value=raw) as creator:
            if wrap_error is None:
                connection.connect()
            else:
                with self.assertRaises(type(wrap_error)):
                    connection.connect()
        return connection, context, raw, creator

    def test_the_socket_targets_the_validated_address_and_port(self):
        connection, _, _, creator = self.connect()

        creator.assert_called_once()
        target, *_ = creator.call_args.args
        self.assertEqual(target, (ADDRESS, PORT))
        self.assertEqual(connection.host, ADDRESS)
        self.assertEqual(connection.port, PORT)

    def test_the_configured_timeout_reaches_socket_creation(self):
        _, _, _, creator = self.connect(timeout=3.25)

        self.assertEqual(creator.call_args.args[1], 3.25)

    def test_tls_receives_the_approved_hostname_as_server_hostname(self):
        """The whole point: the socket is an address, the handshake is a name."""

        connection, context, raw, _ = self.connect()

        self.assertEqual(len(context.calls), 1)
        self.assertEqual(context.calls[0]["server_hostname"], HOSTNAME)
        self.assertIs(context.calls[0]["socket"], raw)
        self.assertNotEqual(context.calls[0]["server_hostname"], ADDRESS)
        self.assertEqual(connection.sni_hostname, HOSTNAME)

    def test_the_wrapped_socket_becomes_the_connection_socket(self):
        connection, context, raw, _ = self.connect()

        self.assertIs(connection.sock, context.wrapped)
        self.assertIsNot(connection.sock, raw)

    def test_an_ipv6_address_is_pinned_the_same_way(self):
        connection, context, _, creator = self.connect(address=ADDRESS_V6)

        self.assertEqual(creator.call_args.args[0], (ADDRESS_V6, PORT))
        self.assertEqual(context.calls[0]["server_hostname"], HOSTNAME)

    def test_the_raw_socket_closes_when_tls_wrapping_fails(self):
        """A leaked socket after a failed handshake is a descriptor leak per attempt."""

        _, _, raw, _ = self.connect(wrap_error=ssl.SSLCertVerificationError("hostname mismatch"))

        self.assertTrue(raw.closed)

    def test_the_raw_socket_closes_for_any_wrapping_failure(self):
        for error in (ssl.SSLError("handshake failed"), OSError("reset"), ValueError("bad context")):
            with self.subTest(error=type(error).__name__):
                _, _, raw, _ = self.connect(wrap_error=error)

                self.assertTrue(raw.closed)

    def test_a_failed_handshake_leaves_no_socket_on_the_connection(self):
        connection, _, _, _ = self.connect(wrap_error=ssl.SSLError("handshake failed"))

        self.assertIsNone(connection.sock)

    def test_connection_setup_resolves_no_hostname(self):
        """The anti-rebinding claim, stated negatively.

        The connection host is an IP literal, so there is nothing left for the
        socket layer to resolve. Patching every resolver the standard library
        would reach proves the address checked is the address used.
        """

        context = RecordingContext()
        connection = PinnedHTTPSConnection(HOSTNAME, ADDRESS, PORT, 9.5, cast(ssl.SSLContext, context))

        # An IP literal by construction, so no lookup is even possible.
        ipaddress.ip_address(connection.host)

        with (
            mock.patch.object(socket, "create_connection", return_value=StubSocket()),
            mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("resolved during connect")) as info,
            mock.patch.object(socket, "gethostbyname", side_effect=AssertionError("resolved during connect")) as byname,
        ):
            connection.connect()

        self.assertEqual(info.call_count, 0)
        self.assertEqual(byname.call_count, 0)

    def test_the_host_header_is_not_set_from_the_pinned_address(self):
        """`http.client` would default `Host` to the IP; callers override it.

        Recorded here so the requirement stays visible next to the pinning: the
        connection cannot supply a correct `Host`, so every caller must.
        """

        context = RecordingContext()
        connection = PinnedHTTPSConnection(HOSTNAME, ADDRESS, PORT, 9.5, cast(ssl.SSLContext, context))

        self.assertEqual(connection.host, ADDRESS)
        self.assertNotEqual(connection.host, HOSTNAME)


class VerifiedContextTests(unittest.TestCase):
    def test_verification_is_asserted_rather_than_assumed(self):
        class Permissive:
            check_hostname = False
            verify_mode = ssl.CERT_NONE

        context = verified_tls_context(cast(Any, Permissive))

        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_the_default_factory_produces_a_verifying_context(self):
        context = verified_tls_context()

        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)


class AddressSelectionTests(unittest.TestCase):
    """`select_validated_address` distinguishes an outage from a policy refusal."""

    def test_a_public_answer_is_returned(self):
        self.assertEqual(select_validated_address(HOSTNAME, PORT, lambda h, p: [ADDRESS]), ADDRESS)

    def test_a_resolver_failure_raises_os_error(self):
        def failing(hostname, port):
            raise OSError("temporary failure in name resolution")

        with self.assertRaises(OSError):
            select_validated_address(HOSTNAME, PORT, failing)

    def test_a_non_public_answer_raises_unsafe_address(self):
        for address in ("127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "203.0.113.1"):
            with self.subTest(address=address):

                def resolve(hostname: str, port: int, chosen: str = address) -> list[str]:
                    return [chosen]

                with self.assertRaises(UnsafeAddress):
                    select_validated_address(HOSTNAME, PORT, resolve)

    def test_a_mixed_answer_is_refused_outright(self):
        with self.assertRaises(UnsafeAddress):
            select_validated_address(HOSTNAME, PORT, lambda h, p: [ADDRESS, "127.0.0.1"])

    def test_an_empty_answer_is_refused(self):
        with self.assertRaises(UnsafeAddress):
            select_validated_address(HOSTNAME, PORT, lambda h, p: [])

    def test_the_two_failures_are_distinguishable_by_type(self):
        """The callers map them to different outcomes, so they must differ."""

        self.assertFalse(issubclass(UnsafeAddress, OSError))

    def test_the_system_resolver_deduplicates_in_order(self):
        records = [
            (socket.AF_INET, None, None, "", (ADDRESS, PORT)),
            (socket.AF_INET, None, None, "", (ADDRESS, PORT)),
            (socket.AF_INET6, None, None, "", (ADDRESS_V6, PORT, 0, 0)),
        ]
        with mock.patch.object(socket, "getaddrinfo", return_value=records):
            self.assertEqual(list(system_resolver(HOSTNAME, PORT)), [ADDRESS, ADDRESS_V6])


if __name__ == "__main__":
    unittest.main()
