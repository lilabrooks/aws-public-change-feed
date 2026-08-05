#!/usr/bin/env python3

"""Screen the live feeds against the configured rules. Requires network.

The labeled corpus is a snapshot. Feeds keep publishing, and a term that fires
on nothing today can fire on next month's boilerplate. This reports every
production match and flags the ones the corpus does not represent, so a new
false positive surfaces as an unlabeled match rather than as a Slack message.

Announcements are fetched and normalized through the runtime acquisition path,
so the text screened here is the text the matcher sees in production. Screening
against differently normalized text is what previously hid a false positive.
"""

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.acquisition import FeedWatcher, load_feeds  # noqa: E402
from aws_public_change_feed.matching import (  # noqa: E402
    Announcement,
    load_risk_rules,
    load_services,
    match_announcement,
)
from aws_public_change_feed.state import InMemoryFeedStateStore  # noqa: E402

CONFIG_PATH = Path("examples/config.yaml")
CORPUS_PATH = Path("corpus/announcements.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Screen live feeds against the configured matching rules.")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument(
        "--fail-on-unlabeled",
        action="store_true",
        help="exit non-zero when a production match is not represented in the corpus",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()

    with (root / CONFIG_PATH).open(encoding="utf-8") as handle:
        configuration = yaml.safe_load(handle)
    with (root / CORPUS_PATH).open(encoding="utf-8") as handle:
        corpus = json.load(handle)

    services = load_services(configuration)
    rules = load_risk_rules(configuration)
    # Derived from the same hostname extraction `validate_feed_url` uses
    # (hostname, casefolded), not from string slicing: `split("/")[2]` kept the
    # port when one was present and preserved case, both of which reject items
    # that the URL policy accepts.
    approved = tuple(
        sorted({(urlsplit(str(feed["url"])).hostname or "").casefold() for feed in configuration.get("feeds", ())})
    )
    known = {entry["canonical_url"].rstrip("/") for entry in corpus["items"]}

    watcher = FeedWatcher(approved_hosts=approved, state=InMemoryFeedStateStore())
    result = watcher.run(list(load_feeds(configuration)))

    for outcome in result.outcomes:
        detail = f" ({outcome.error_class}: {outcome.detail})" if outcome.error_class else ""
        print(f"{outcome.feed_name}: {outcome.status} items={outcome.item_count}{detail}")

    print(f"\nscreened {len(result.announcements)} normalized announcements")

    unlabeled = []
    for announcement in result.announcements:
        matches = match_announcement(Announcement(announcement.title, announcement.summary), services, rules)
        if not matches:
            continue
        labeled = announcement.canonical_url.rstrip("/") in known
        pairs = sorted(f"{match.service_id}/{match.risk_type}" for match in matches)
        terms = sorted({term for match in matches for term in match.matched_terms})
        marker = "labeled" if labeled else "UNLABELED"
        print(f"  [{marker}] {pairs} terms={terms}")
        print(f"            {announcement.title[:90]}")
        if not labeled:
            unlabeled.append(announcement)

    print(f"\nmatches not represented in the corpus: {len(unlabeled)}")
    if unlabeled:
        print("review each one, then label it into the corpus or correct the rule that fired.")
        for announcement in unlabeled:
            print(f"  {announcement.canonical_url}")

    if unlabeled and args.fail_on_unlabeled:
        return 1
    if result.failed_feeds:
        print(f"\nfeeds that failed this run: {', '.join(result.failed_feeds)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
