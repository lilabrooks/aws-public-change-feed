"""Feed URL and network-address policy.

Chapter 04 requires the watcher to parse the URL and demand HTTPS, the default
port, an approved lowercase hostname, an empty user-info component, and a
bounded path and query. It then requires every resolved address to be rejected
if it is loopback, link-local, private, multicast, reserved, unspecified, or
otherwise non-global, on every connection.

Both checks live here so the fetcher cannot accidentally skip one.
"""

from __future__ import annotations

import ipaddress
import string
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

__all__ = [
    "FeedUrlRejected",
    "MAX_PATH_CHARACTERS",
    "MAX_QUERY_CHARACTERS",
    "UnsafeAddress",
    "ValidatedUrl",
    "validate_addresses",
    "validate_feed_url",
    "validate_uri_spelling",
    "validate_source_item_url",
]

MAX_PATH_CHARACTERS = 512
MAX_QUERY_CHARACTERS = 512
_URI_CHARACTERS = frozenset(string.ascii_letters + string.digits + "-._~:/?#[]@!$&'()*+,;=%")


class FeedUrlRejected(ValueError):
    """A configured feed URL failed policy before any network use."""


class UnsafeAddress(ValueError):
    """A resolved address is not a public destination."""


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    """A feed URL that passed policy, split into the parts the fetcher needs."""

    url: str
    hostname: str
    port: int
    path_with_query: str


def validate_uri_spelling(raw_url: str) -> None:
    """Reject raw characters that URL parsing would silently repair or drop."""

    if any(character not in _URI_CHARACTERS for character in raw_url):
        raise FeedUrlRejected("URL contains a character that must be percent-encoded")
    for index, character in enumerate(raw_url):
        if character == "%" and (
            index + 2 >= len(raw_url) or any(digit not in string.hexdigits for digit in raw_url[index + 1 : index + 3])
        ):
            raise FeedUrlRejected("URL contains a malformed percent escape")


def validate_feed_url(raw_url: str, approved_hosts: Iterable[str]) -> ValidatedUrl:
    """Apply URL policy, or raise ``FeedUrlRejected``."""

    validate_uri_spelling(raw_url)

    try:
        parsed = urlsplit(raw_url)
    except ValueError as error:
        raise FeedUrlRejected(f"feed URL is malformed: {error}") from error

    if parsed.scheme != "https":
        raise FeedUrlRejected(f"feed URL scheme must be https: {parsed.scheme or '<empty>'}")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise FeedUrlRejected("feed URL must not carry a user-info component")

    hostname = parsed.hostname or ""
    if not hostname:
        raise FeedUrlRejected("feed URL must contain a hostname")
    # `parsed.hostname` is already lowercased by urlsplit, so the configured
    # spelling has to be checked against the raw netloc or the rule is dead.
    raw_host = parsed.netloc.rpartition("@")[2].partition(":")[0]
    if raw_host != raw_host.casefold():
        raise FeedUrlRejected(f"feed URL hostname must be lowercase: {raw_host}")

    approved = {host.casefold() for host in approved_hosts}
    if hostname not in approved:
        raise FeedUrlRejected(f"feed URL hostname is not approved: {hostname}")

    try:
        port = parsed.port
    except ValueError as error:  # malformed port text
        raise FeedUrlRejected(f"feed URL port is malformed: {error}") from error
    if port not in (None, 443):
        raise FeedUrlRejected(f"feed URL must use the default HTTPS port: {port}")

    path = parsed.path or "/"
    if len(path) > MAX_PATH_CHARACTERS:
        raise FeedUrlRejected(f"feed URL path exceeds {MAX_PATH_CHARACTERS} characters")
    if len(parsed.query) > MAX_QUERY_CHARACTERS:
        raise FeedUrlRejected(f"feed URL query exceeds {MAX_QUERY_CHARACTERS} characters")
    if parsed.fragment:
        raise FeedUrlRejected("feed URL must not carry a fragment")

    return ValidatedUrl(
        url=raw_url,
        hostname=hostname,
        port=443,
        path_with_query=f"{path}?{parsed.query}" if parsed.query else path,
    )


def validate_source_item_url(raw_url: str, approved_hosts: Iterable[str]) -> str:
    """Return one accepted item's canonical URL, or raise ``FeedUrlRejected``.

    Item links may carry the spelling differences chapter 04 canonicalizes.
    Their raw value still becomes provenance, so malformed escapes, user info,
    whitespace, controls, and unencoded Unicode are refused before that value
    can become durable.
    """

    from .identity import canonical_public_url

    validate_uri_spelling(raw_url)
    try:
        parsed = urlsplit(raw_url)
        if parsed.username or parsed.password or "@" in parsed.netloc:
            raise FeedUrlRejected("source item URL must not carry a user-info component")
        canonical = canonical_public_url(raw_url)
    except ValueError as error:
        raise FeedUrlRejected(f"source item URL is malformed: {error}") from error

    try:
        validate_feed_url(canonical, approved_hosts)
    except FeedUrlRejected as error:
        raise FeedUrlRejected(f"source item URL failed canonical HTTPS policy: {error}") from error
    return canonical


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if any(
        (
            address.is_loopback,
            address.is_link_local,
            address.is_private,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    ):
        return False
    # is_global additionally rejects shared address space, benchmarking ranges,
    # and IPv6 transition prefixes that the flags above do not name.
    return bool(address.is_global)


def validate_addresses(candidates: Sequence[str]) -> tuple[str, ...]:
    """Return the resolved addresses, or raise if any one of them is unsafe.

    Every address is checked, not merely the one chosen for the connection. A
    name that resolves to one public and one private address is rejected
    outright, which is what closes the DNS-rebinding path.
    """

    if not candidates:
        raise UnsafeAddress("feed hostname did not resolve to any address")

    validated: list[str] = []
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError as error:
            raise UnsafeAddress(f"resolved address is not an IP address: {candidate}") from error
        if not _is_public(address):
            raise UnsafeAddress(f"resolved address is not a public destination: {candidate}")
        validated.append(str(address))
    return tuple(validated)
