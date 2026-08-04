import sys
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.acquisition import FeedDefinition, FeedWatcher  # noqa: E402
from aws_public_change_feed.announcements import (  # noqa: E402
    MAX_SUMMARY_CHARACTERS,
    Provenance,
    coalesce,
    normalize_item,
    parse_published,
    sanitize,
)
from aws_public_change_feed.feedparse import FeedParseRejected, parse_feed  # noqa: E402
from aws_public_change_feed.fetching import FeedFetcher, FetchRejected  # noqa: E402
from aws_public_change_feed.identity import canonical_public_url  # noqa: E402
from aws_public_change_feed.state import FeedCheckpoint, InMemoryFeedStateStore, InMemorySnapshotStore  # noqa: E402
from aws_public_change_feed.urls import (  # noqa: E402
    FeedUrlRejected,
    UnsafeAddress,
    validate_addresses,
    validate_feed_url,
)

APPROVED = ("aws.amazon.com",)
OBSERVED = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Amazon EKS end of support</title>
    <link>https://aws.amazon.com/one/</link>
    <description>Cluster owners must act.</description>
    <pubDate>Tue, 01 Jul 2026 10:00:00 GMT</pubDate>
  </item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Amazon RDS engine versions</title>
    <link rel="alternate" href="https://aws.amazon.com/two/"/>
    <summary>Details.</summary>
    <updated>2026-07-02T10:00:00Z</updated>
  </entry>
</feed>"""

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>
<rss version="2.0"><channel><item><title>&lol2;</title>
<link>https://aws.amazon.com/x/</link></item></channel></rss>"""

EXTERNAL_ENTITY = b"""<?xml version="1.0"?>
<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<rss version="2.0"><channel><item><title>&xxe;</title>
<link>https://aws.amazon.com/x/</link></item></channel></rss>"""

EXTERNAL_DTD = b"""<?xml version="1.0"?>
<!DOCTYPE rss SYSTEM "https://evil.example/evil.dtd">
<rss version="2.0"><channel><item><title>t</title>
<link>https://aws.amazon.com/x/</link></item></channel></rss>"""


def stored(state, feed_name):
    """Load a checkpoint that the test knows exists."""

    checkpoint = state.load(feed_name)
    assert checkpoint is not None
    return checkpoint


class UrlPolicyTests(unittest.TestCase):
    def test_valid_url_is_accepted(self):
        target = validate_feed_url("https://aws.amazon.com/feed/?x=1", APPROVED)
        self.assertEqual(target.hostname, "aws.amazon.com")
        self.assertEqual(target.port, 443)
        self.assertEqual(target.path_with_query, "/feed/?x=1")

    def test_rejections(self):
        cases = {
            "http scheme": "http://aws.amazon.com/feed/",
            "userinfo": "https://user:pass@aws.amazon.com/feed/",
            "unapproved host": "https://evil.example/feed/",
            "uppercase host": "https://AWS.amazon.com/feed/",
            "non-default port": "https://aws.amazon.com:8443/feed/",
            "fragment": "https://aws.amazon.com/feed/#x",
            "long path": "https://aws.amazon.com/" + "a" * 600,
            "long query": "https://aws.amazon.com/feed/?" + "a" * 600,
            "no host": "https:///feed/",
        }
        for label, url in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(FeedUrlRejected):
                    validate_feed_url(url, APPROVED)


class AddressPolicyTests(unittest.TestCase):
    def test_public_addresses_are_accepted(self):
        self.assertEqual(validate_addresses(["93.184.216.34"]), ("93.184.216.34",))

    def test_unsafe_addresses_are_rejected(self):
        cases = {
            "loopback": "127.0.0.1",
            "private": "10.0.0.5",
            "link local": "169.254.169.254",
            "multicast": "224.0.0.1",
            "unspecified": "0.0.0.0",
            "ipv6 loopback": "::1",
            "ipv6 unique local": "fd00::1",
            "not an address": "not-an-ip",
        }
        for label, address in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(UnsafeAddress):
                    validate_addresses([address])

    def test_every_resolved_address_must_pass(self):
        # A name resolving to one public and one private address is the
        # DNS-rebinding shape, so the whole resolution is refused.
        with self.assertRaises(UnsafeAddress):
            validate_addresses(["93.184.216.34", "127.0.0.1"])

    def test_empty_resolution_is_rejected(self):
        with self.assertRaises(UnsafeAddress):
            validate_addresses([])


@dataclass
class FakeResponse:
    status: int
    body: bytes = b""
    headers: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.headers is None:
            self.headers = {}

    def read(self, limit=None):
        return self.body if limit is None else self.body[:limit]


class FakeConnection:
    """Stands in for the pinned HTTPS connection."""

    def __init__(self, response, recorder=None):
        self._response = response
        self._recorder = recorder if recorder is not None else {}

    def __call__(self, hostname, address, port, timeout, context):
        self._recorder["hostname"] = hostname
        self._recorder["address"] = address
        self._recorder["context"] = context
        return self

    def request(self, method, path, headers=None):
        self._recorder["method"] = method
        self._recorder["path"] = path
        self._recorder["headers"] = headers or {}

    def getresponse(self):
        return self._response

    def close(self):
        self._recorder["closed"] = True


def fetcher_for(response, resolver=None, recorder=None):
    return FeedFetcher(
        resolver=resolver or (lambda hostname, port: ["93.184.216.34"]),
        connection_factory=FakeConnection(response, recorder),
    )


class FetchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.target = validate_feed_url("https://aws.amazon.com/feed/", APPROVED)

    def ok_headers(self, **extra):
        headers = {"Content-Type": "application/rss+xml", "ETag": '"v1"'}
        headers.update(extra)
        return headers

    def test_successful_fetch_returns_validators(self):
        recorder: dict = {}
        outcome = fetcher_for(FakeResponse(200, RSS, self.ok_headers()), recorder=recorder).fetch(self.target)
        self.assertEqual(outcome.status, 200)
        self.assertEqual(outcome.etag, '"v1"')
        self.assertEqual(outcome.address, "93.184.216.34")

    def test_connection_pins_address_but_keeps_hostname(self):
        recorder: dict = {}
        fetcher_for(FakeResponse(200, RSS, self.ok_headers()), recorder=recorder).fetch(self.target)
        self.assertEqual(recorder["address"], "93.184.216.34")
        self.assertEqual(recorder["hostname"], "aws.amazon.com")
        self.assertEqual(recorder["headers"]["Host"], "aws.amazon.com")
        self.assertTrue(recorder["context"].check_hostname)

    def test_conditional_headers_are_sent_when_known(self):
        recorder: dict = {}
        fetcher_for(FakeResponse(304, headers={}), recorder=recorder).fetch(
            self.target, etag='"v1"', last_modified="Tue, 01 Jul 2026 10:00:00 GMT"
        )
        self.assertEqual(recorder["headers"]["If-None-Match"], '"v1"')
        self.assertEqual(recorder["headers"]["If-Modified-Since"], "Tue, 01 Jul 2026 10:00:00 GMT")

    def test_compression_is_not_advertised(self):
        recorder: dict = {}
        fetcher_for(FakeResponse(200, RSS, self.ok_headers()), recorder=recorder).fetch(self.target)
        self.assertEqual(recorder["headers"]["Accept-Encoding"], "identity")

    def test_not_modified_carries_no_body(self):
        outcome = fetcher_for(FakeResponse(304)).fetch(self.target)
        self.assertTrue(outcome.not_modified)
        self.assertEqual(outcome.body, b"")

    def test_redirects_are_refused(self):
        for status in (301, 302, 307, 308):
            with self.subTest(status=status):
                with self.assertRaises(FetchRejected) as caught:
                    fetcher_for(FakeResponse(status, headers={"Location": "https://evil.example/"})).fetch(self.target)
                self.assertEqual(caught.exception.reason_class, "redirect")

    def test_unexpected_status_is_refused(self):
        with self.assertRaises(FetchRejected) as caught:
            fetcher_for(FakeResponse(500)).fetch(self.target)
        self.assertEqual(caught.exception.reason_class, "status")

    def test_unsupported_content_type_is_refused(self):
        with self.assertRaises(FetchRejected) as caught:
            fetcher_for(FakeResponse(200, RSS, {"Content-Type": "text/html"})).fetch(self.target)
        self.assertEqual(caught.exception.reason_class, "content_type")

    def test_charset_parameter_is_tolerated(self):
        outcome = fetcher_for(FakeResponse(200, RSS, {"Content-Type": "application/rss+xml; charset=utf-8"})).fetch(
            self.target
        )
        self.assertEqual(outcome.status, 200)

    def test_compressed_response_is_refused(self):
        with self.assertRaises(FetchRejected) as caught:
            fetcher_for(FakeResponse(200, RSS, self.ok_headers(**{"Content-Encoding": "gzip"}))).fetch(self.target)
        self.assertEqual(caught.exception.reason_class, "content_encoding")

    def test_declared_oversize_is_refused_before_reading(self):
        headers = self.ok_headers(**{"Content-Length": str(50 * 1024 * 1024)})
        with self.assertRaises(FetchRejected) as caught:
            fetcher_for(FakeResponse(200, RSS, headers)).fetch(self.target)
        self.assertEqual(caught.exception.reason_class, "response_limit")

    def test_undeclared_oversize_body_is_refused(self):
        fetcher = fetcher_for(FakeResponse(200, b"x" * 200, self.ok_headers()))
        fetcher.max_response_bytes = 100
        with self.assertRaises(FetchRejected) as caught:
            fetcher.fetch(self.target)
        self.assertEqual(caught.exception.reason_class, "response_limit")

    def test_body_exactly_at_the_limit_is_accepted(self):
        fetcher = fetcher_for(FakeResponse(200, b"x" * 100, self.ok_headers()))
        fetcher.max_response_bytes = 100
        self.assertEqual(len(fetcher.fetch(self.target).body), 100)

    def test_unsafe_resolution_refuses_before_connecting(self):
        recorder: dict = {}
        fetcher = fetcher_for(FakeResponse(200, RSS), resolver=lambda h, p: ["127.0.0.1"], recorder=recorder)
        with self.assertRaises(FetchRejected) as caught:
            fetcher.fetch(self.target)
        self.assertEqual(caught.exception.reason_class, "dns")
        self.assertNotIn("address", recorder)

    def test_resolver_failure_is_a_dns_class(self):
        def failing(hostname, port):
            raise OSError("no such host")

        with self.assertRaises(FetchRejected) as caught:
            fetcher_for(FakeResponse(200, RSS), resolver=failing).fetch(self.target)
        self.assertEqual(caught.exception.reason_class, "dns")


class ParserSafetyTests(unittest.TestCase):
    def test_rss_and_atom_both_parse(self):
        self.assertEqual(parse_feed(RSS)[0].url, "https://aws.amazon.com/one/")
        self.assertEqual(parse_feed(ATOM)[0].url, "https://aws.amazon.com/two/")

    def test_doctype_bearing_documents_are_refused(self):
        for label, body in (
            ("billion laughs", BILLION_LAUGHS),
            ("external entity", EXTERNAL_ENTITY),
            ("external dtd", EXTERNAL_DTD),
        ):
            with self.subTest(label=label):
                with self.assertRaises(FeedParseRejected) as caught:
                    parse_feed(body)
                self.assertEqual(caught.exception.reason_class, "parser")

    def test_malformed_xml_is_refused(self):
        with self.assertRaises(FeedParseRejected):
            parse_feed(b"<rss><channel><item></rss>")

    def test_empty_body_is_refused(self):
        with self.assertRaises(FeedParseRejected):
            parse_feed(b"   ")

    def test_unsupported_root_is_refused(self):
        with self.assertRaises(FeedParseRejected):
            parse_feed(b"<html><body>hello</body></html>")

    def test_excessive_items_are_refused(self):
        entry = b"<item><title>t</title><link>https://aws.amazon.com/x/</link></item>"
        body = b"<rss version='2.0'><channel>" + entry * 501 + b"</channel></rss>"
        with self.assertRaises(FeedParseRejected):
            parse_feed(body)

    def test_oversized_item_is_refused(self):
        body = (
            b"<rss version='2.0'><channel><item><title>t</title>"
            b"<link>https://aws.amazon.com/x/</link><description>"
            + b"x" * 25_000
            + b"</description></item></channel></rss>"
        )
        with self.assertRaises(FeedParseRejected):
            parse_feed(body)

    def test_item_without_link_or_title_is_dropped_not_fatal(self):
        body = (
            b"<rss version='2.0'><channel>"
            b"<item><title>no link</title></item>"
            b"<item><link>https://aws.amazon.com/x/</link></item>"
            b"<item><title>keep</title><link>https://aws.amazon.com/y/</link></item>"
            b"</channel></rss>"
        )
        self.assertEqual([item.title for item in parse_feed(body)], ["keep"])


class NormalizationTests(unittest.TestCase):
    def test_html_is_sanitized_to_text(self):
        self.assertEqual(sanitize("<p>Hello <b>world</b></p>", 100), "Hello world")

    def test_entity_encoded_markup_does_not_survive(self):
        self.assertEqual(sanitize("&lt;script&gt;alert(1)&lt;/script&gt;", 100), "alert(1)")

    def test_truncation_carries_an_explicit_marker(self):
        text = sanitize("x" * (MAX_SUMMARY_CHARACTERS + 50), MAX_SUMMARY_CHARACTERS)
        self.assertTrue(text.endswith("…"))
        self.assertLessEqual(len(text), MAX_SUMMARY_CHARACTERS + 1)

    def test_publication_time_formats(self):
        self.assertIsNotNone(parse_published("Tue, 01 Jul 2026 10:00:00 GMT"))
        self.assertIsNotNone(parse_published("2026-07-01T10:00:00Z"))

    def test_missing_or_unparseable_publication_time_is_absent_not_fatal(self):
        self.assertIsNone(parse_published(None))
        self.assertIsNone(parse_published(""))
        self.assertIsNone(parse_published("last Thursday"))

    def test_normalized_item_carries_deterministic_identity(self):
        item = parse_feed(RSS)[0]
        first = normalize_item(item, "feed-a", OBSERVED)
        second = normalize_item(item, "feed-a", OBSERVED)
        assert first is not None and second is not None
        self.assertEqual(first.announcement_id, second.announcement_id)
        self.assertEqual(first.revision_id, second.revision_id)
        self.assertEqual(first.source_type, "public_feed")

    def test_title_change_produces_a_new_revision_but_same_announcement(self):
        item = parse_feed(RSS)[0]
        original = normalize_item(item, "feed-a", OBSERVED)
        edited = normalize_item(
            type(item)(url=item.url, title="Amazon EKS end of support (updated)", summary=item.summary),
            "feed-a",
            OBSERVED,
        )
        assert original is not None and edited is not None
        self.assertEqual(original.announcement_id, edited.announcement_id)
        self.assertNotEqual(original.revision_id, edited.revision_id)

    def test_tracking_parameters_do_not_split_an_announcement(self):
        plain = canonical_public_url("https://aws.amazon.com/one/")
        tagged = canonical_public_url("https://aws.amazon.com/one/?utm_source=x&trk=y")
        self.assertEqual(plain, tagged)

    def test_non_https_item_is_dropped(self):
        item = type(parse_feed(RSS)[0])(url="http://aws.amazon.com/one/", title="t", summary="")
        self.assertIsNone(normalize_item(item, "feed-a", OBSERVED))


class CoalescingTests(unittest.TestCase):
    def build(self, feed_name, title="Title", summary="", published=None, url="https://aws.amazon.com/one/"):
        from aws_public_change_feed.feedparse import ParsedItem

        return normalize_item(
            ParsedItem(url=url, title=title, summary=summary, published_raw=published), feed_name, OBSERVED
        )

    def test_overlapping_feeds_produce_one_announcement_with_merged_provenance(self):
        merged = coalesce([self.build("feed-b"), self.build("feed-a")])
        self.assertEqual(len(merged), 1)
        self.assertEqual(
            merged[0].provenance,
            (
                Provenance("feed-a", "https://aws.amazon.com/one/"),
                Provenance("feed-b", "https://aws.amazon.com/one/"),
            ),
        )

    def test_merge_does_not_depend_on_feed_order(self):
        forward = coalesce([self.build("feed-a"), self.build("feed-b")])
        backward = coalesce([self.build("feed-b"), self.build("feed-a")])
        self.assertEqual(forward, backward)

    def test_earlier_publication_time_wins_and_absent_loses(self):
        merged = coalesce(
            [
                self.build("feed-a", title="Later", published="Wed, 02 Jul 2026 10:00:00 GMT"),
                self.build("feed-b", title="Earlier", published="Tue, 01 Jul 2026 10:00:00 GMT"),
                self.build("feed-c", title="Undated"),
            ]
        )
        self.assertEqual(merged[0].title, "Earlier")
        self.assertEqual(len(merged[0].provenance), 3)

    def test_distinct_urls_stay_distinct(self):
        merged = coalesce([self.build("feed-a"), self.build("feed-a", url="https://aws.amazon.com/two/")])
        self.assertEqual(len(merged), 2)


class WatcherTests(unittest.TestCase):
    def feeds(self, *names):
        return [
            FeedDefinition(name=name, url="https://aws.amazon.com/feed/", source_type="public_rss") for name in names
        ]

    def watcher(self, response, state=None, snapshots=None):
        return FeedWatcher(
            approved_hosts=APPROVED,
            state=state or InMemoryFeedStateStore(),
            fetcher=fetcher_for(response),
            snapshots=snapshots,
            clock=lambda: OBSERVED,
        )

    def test_successful_run_holds_the_checkpoint_until_commit(self):
        state = InMemoryFeedStateStore()
        watcher = self.watcher(FakeResponse(200, RSS, {"Content-Type": "application/rss+xml", "ETag": '"v2"'}), state)
        result = watcher.run(self.feeds("aws-whats-new"))

        self.assertEqual(len(result.announcements), 1)
        # The attempt is visible for alarms, but the validators have not moved,
        # so a crash here replays the response rather than skipping it.
        self.assertEqual(stored(state, "aws-whats-new").last_attempt_at, OBSERVED.isoformat())
        self.assertIsNone(stored(state, "aws-whats-new").etag)
        self.assertIsNone(stored(state, "aws-whats-new").last_success_at)

        watcher.commit(result)
        self.assertEqual(stored(state, "aws-whats-new").etag, '"v2"')

    def test_not_modified_advances_success_without_work(self):
        state = InMemoryFeedStateStore()
        state.save(FeedCheckpoint(feed_name="f", feed_url="https://aws.amazon.com/feed/", etag='"v1"'))
        result = self.watcher(FakeResponse(304), state).run(self.feeds("f"))

        self.assertEqual(result.outcomes[0].status, "not_modified")
        self.assertEqual(result.announcements, ())
        self.assertEqual(stored(state, "f").last_success_at, OBSERVED.isoformat())
        self.assertEqual(stored(state, "f").etag, '"v1"')

    def test_failed_feed_keeps_its_prior_validators(self):
        state = InMemoryFeedStateStore()
        state.save(
            FeedCheckpoint(feed_name="f", feed_url="https://aws.amazon.com/feed/", etag='"v1"', last_modified="then")
        )
        self.watcher(FakeResponse(500), state).run(self.feeds("f"))

        record = stored(state, "f")
        self.assertEqual(record.etag, '"v1"')
        self.assertEqual(record.last_modified, "then")
        self.assertEqual(record.consecutive_failures, 1)
        self.assertEqual(record.last_error_class, "status")

    def test_one_failed_feed_does_not_block_the_others(self):
        state = InMemoryFeedStateStore()

        class Mixed:
            def __init__(self):
                self.calls = 0

            def fetch(self, target, etag=None, last_modified=None):
                self.calls += 1
                if self.calls == 1:
                    raise FetchRejected("tls", "handshake failed")
                from aws_public_change_feed.fetching import FetchOutcome

                return FetchOutcome(status=200, body=RSS, etag='"v9"')

        watcher = FeedWatcher(approved_hosts=APPROVED, state=state, fetcher=Mixed(), clock=lambda: OBSERVED)
        result = watcher.run(self.feeds("broken", "healthy"))

        self.assertEqual(result.failed_feeds, ("broken",))
        self.assertEqual(len(result.announcements), 1)
        self.assertEqual(watcher.commit(result), ("healthy",))

    def test_unapproved_host_fails_the_feed_without_fetching(self):
        state = InMemoryFeedStateStore()
        watcher = self.watcher(FakeResponse(200, RSS), state)
        feeds = [FeedDefinition(name="bad", url="https://evil.example/feed/", source_type="public_rss")]
        result = watcher.run(feeds)
        self.assertEqual(result.outcomes[0].error_class, "url")

    def test_snapshot_is_stored_when_configured(self):
        snapshots = InMemorySnapshotStore()
        watcher = self.watcher(FakeResponse(200, RSS, {"Content-Type": "application/rss+xml"}), snapshots=snapshots)
        result = watcher.run(self.feeds("f"))
        self.assertEqual(len(snapshots.snapshots), 1)
        self.assertIsNotNone(result.outcomes[0].snapshot_key)

    def test_replay_produces_identical_identities(self):
        first = self.watcher(FakeResponse(200, RSS, {"Content-Type": "application/rss+xml"})).run(self.feeds("f"))
        second = self.watcher(FakeResponse(200, RSS, {"Content-Type": "application/rss+xml"})).run(self.feeds("f"))
        self.assertEqual(
            [entry.revision_id for entry in first.announcements],
            [entry.revision_id for entry in second.announcements],
        )

    def test_newest_publication_time_is_recorded(self):
        state = InMemoryFeedStateStore()
        watcher = self.watcher(FakeResponse(200, RSS, {"Content-Type": "application/rss+xml"}), state)
        watcher.commit(watcher.run(self.feeds("f")))
        self.assertEqual(stored(state, "f").newest_publication_at, "2026-07-01T10:00:00+00:00")


class SharedIdentityTests(unittest.TestCase):
    def test_validator_and_runtime_share_one_implementation(self):
        # Chapter 04 requires one framing helper for runtime and test vectors.
        sys.path.insert(0, str(ROOT / "scripts"))
        import validate_config

        from aws_public_change_feed import identity

        self.assertIs(validate_config.canonical_public_url, identity.canonical_public_url)
        self.assertIs(validate_config.digest_parts, identity.digest_parts)


if __name__ == "__main__":
    unittest.main()
