"""Profile mapping and candidate construction, bound to the canonical bundle.

`examples/alert-candidate.json` commits a candidate that `examples/config.yaml`
and `examples/inventory.json` fully determine. These tests rebuild it from those
two files and require the runtime to reproduce the committed document field for
field, so a mapping or derivation change cannot pass by agreeing with itself.

The mapping tests mutate a copy of the real release rather than a hand-built
dictionary. A fixture that states its condition against the shipped
configuration keeps testing that configuration when it changes.
"""

import copy
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.announcements import (  # noqa: E402
    NormalizedAnnouncement,
    Provenance,
)
from aws_public_change_feed.candidates import (  # noqa: E402
    build_candidate,
    build_candidates,
    explainability_reason,
    utc_timestamp,
)
from aws_public_change_feed.matching import (  # noqa: E402
    Announcement,
    load_risk_rules,
    load_services,
    match_announcement,
)
from aws_public_change_feed.profiles import route_audiences  # noqa: E402


def load_json(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_config():
    with (ROOT / "examples" / "config.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def parse_timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class FixtureBase(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.inventory = load_json("inventory.json")
        self.candidate = load_json("alert-candidate.json")
        self.source = self.candidate["announcement"]


class RouteAudienceTests(FixtureBase):
    """Chapter 04 environment and route mapping."""

    def test_reproduces_the_committed_audience(self):
        audiences = route_audiences(self.config, self.inventory, self.candidate["service"]["id"])
        self.assertEqual(len(audiences), 1)
        audience = audiences[0]
        self.assertEqual(audience.route_id, self.candidate["route_id"])
        self.assertEqual(list(audience.environment_ids), self.candidate["environment_ids"])
        self.assertEqual(
            list(audience.profile_ids),
            self.candidate["explainability"]["matched_profile_ids"],
        )
        self.assertEqual(
            audience.audience_fingerprint,
            self.candidate["audience_fingerprint"],
        )

    def test_environment_ids_are_sorted_regardless_of_inventory_order(self):
        reversed_inventory = copy.deepcopy(self.inventory)
        reversed_inventory["environments"].reverse()
        forward = route_audiences(self.config, self.inventory, "eks")
        backward = route_audiences(self.config, reversed_inventory, "eks")
        self.assertEqual(forward, backward)
        self.assertEqual(
            backward[0].audience_fingerprint,
            self.candidate["audience_fingerprint"],
        )

    def test_disabled_environments_never_appear(self):
        config = copy.deepcopy(self.config)
        config["environment_policies"]["globex-staging"]["feed_monitoring"] = "disabled"
        audience = route_audiences(config, self.inventory, "eks")[0]
        self.assertNotIn("globex-staging", audience.environment_ids)
        self.assertNotEqual(
            audience.audience_fingerprint,
            self.candidate["audience_fingerprint"],
            msg="a changed environment set must change the audience fingerprint",
        )

    def test_unconfigured_environments_never_appear(self):
        config = copy.deepcopy(self.config)
        del config["environment_policies"]["globex-staging"]
        audience = route_audiences(config, self.inventory, "eks")[0]
        self.assertNotIn("globex-staging", audience.environment_ids)

    def test_service_outside_the_profile_yields_no_audience(self):
        config = copy.deepcopy(self.config)
        config["service_profiles"]["standard-customer-stack"]["service_ids"] = ["rds"]
        self.assertEqual(route_audiences(config, self.inventory, "eks"), ())

    def test_environments_group_by_route(self):
        config = copy.deepcopy(self.config)
        inventory = copy.deepcopy(self.inventory)
        inventory["slack"]["routes"]["globex-alerts"] = copy.deepcopy(inventory["slack"]["routes"]["shared-alerts"])
        inventory["slack"]["routes"]["globex-alerts"]["destination_key"] = "globex-aws-alerts"
        for environment in inventory["environments"]:
            if environment["customer"] == "Globex":
                environment["route_id"] = "globex-alerts"

        audiences = route_audiences(config, inventory, "eks")
        self.assertEqual(
            [(entry.route_id, list(entry.environment_ids)) for entry in audiences],
            [
                ("globex-alerts", ["globex-prod", "globex-staging"]),
                ("shared-alerts", ["acme-prod"]),
            ],
            msg="routes are ordered by route ID and never share environments",
        )

    def test_unknown_route_is_rejected(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["environments"][0]["route_id"] = "absent-route"
        with self.assertRaises(ValueError):
            route_audiences(self.config, inventory, "eks")

    def test_unknown_profile_is_rejected(self):
        config = copy.deepcopy(self.config)
        config["environment_policies"]["acme-prod"]["profile"] = "absent-profile"
        with self.assertRaises(ValueError):
            route_audiences(config, self.inventory, "eks")


class BuildCandidateTests(FixtureBase):
    """The committed candidate, rebuilt from the release that determines it."""

    def build(self):
        announcement = NormalizedAnnouncement(
            canonical_url=self.source["url"],
            title=self.source["title"],
            summary=self.source["summary"],
            observed_at=parse_timestamp(self.source["observed_at"]),
            published_at=parse_timestamp(self.source["published_at"]),
            provenance=tuple(
                Provenance(feed_name=item["feed_name"], item_url=item["source_item_url"])
                for item in self.source["provenance"]
            ),
        )
        matches = match_announcement(
            Announcement(title=announcement.title, summary=announcement.summary),
            load_services(self.config),
            load_risk_rules(self.config),
        )
        match = next(
            result
            for result in matches
            if result.pair == (self.candidate["service"]["id"], self.candidate["risk"]["risk_type"])
        )
        audience = next(
            entry
            for entry in route_audiences(self.config, self.inventory, match.service_id)
            if entry.route_id == self.candidate["route_id"]
        )
        return build_candidate(
            announcement=announcement,
            match=match,
            audience=audience,
            configuration=self.config,
            release=self.candidate["release"],
            created_at=parse_timestamp(self.candidate["created_at"]),
            is_update=self.source["is_update"],
        )

    def test_rebuilds_the_committed_candidate(self):
        self.assertEqual(self.build(), self.candidate)

    def test_the_delivery_request_embeds_the_same_candidate(self):
        self.assertEqual(self.build(), load_json("delivery-request.json")["candidate"])

    def test_release_metadata_is_copied_not_aliased(self):
        built = self.build()
        built["release"]["application_version"] = "mutated"
        self.assertNotEqual(
            self.candidate["release"]["application_version"],
            "mutated",
            msg="the candidate must not alias the release mapping it was given",
        )

    def test_absent_publication_time_omits_the_key(self):
        announcement = NormalizedAnnouncement(
            canonical_url=self.source["url"],
            title=self.source["title"],
            summary=self.source["summary"],
            observed_at=parse_timestamp(self.source["observed_at"]),
            published_at=None,
            provenance=(
                Provenance(
                    feed_name=self.source["provenance"][0]["feed_name"],
                    item_url=self.source["provenance"][0]["source_item_url"],
                ),
            ),
        )
        match = next(
            result
            for result in match_announcement(
                Announcement(title=announcement.title, summary=announcement.summary),
                load_services(self.config),
                load_risk_rules(self.config),
            )
            if result.pair == ("eks", "service-version-update")
        )
        built = build_candidate(
            announcement=announcement,
            match=match,
            audience=route_audiences(self.config, self.inventory, "eks")[0],
            configuration=self.config,
            release=self.candidate["release"],
            created_at=parse_timestamp(self.candidate["created_at"]),
        )
        self.assertNotIn("published_at", built["announcement"])

    def test_one_candidate_per_route_with_distinct_identities(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["slack"]["routes"]["globex-alerts"] = copy.deepcopy(inventory["slack"]["routes"]["shared-alerts"])
        inventory["slack"]["routes"]["globex-alerts"]["destination_key"] = "globex-aws-alerts"
        for environment in inventory["environments"]:
            if environment["customer"] == "Globex":
                environment["route_id"] = "globex-alerts"

        announcement = NormalizedAnnouncement(
            canonical_url=self.source["url"],
            title=self.source["title"],
            summary=self.source["summary"],
            observed_at=parse_timestamp(self.source["observed_at"]),
            published_at=parse_timestamp(self.source["published_at"]),
            provenance=(
                Provenance(
                    feed_name=self.source["provenance"][0]["feed_name"],
                    item_url=self.source["provenance"][0]["source_item_url"],
                ),
            ),
        )
        match = next(
            result
            for result in match_announcement(
                Announcement(title=announcement.title, summary=announcement.summary),
                load_services(self.config),
                load_risk_rules(self.config),
            )
            if result.pair == ("eks", "service-version-update")
        )
        built = build_candidates(
            announcement=announcement,
            match=match,
            audiences=route_audiences(self.config, inventory, "eks"),
            configuration=self.config,
            release=self.candidate["release"],
            created_at=parse_timestamp(self.candidate["created_at"]),
        )
        self.assertEqual([item["route_id"] for item in built], ["globex-alerts", "shared-alerts"])
        self.assertEqual(
            [item["environment_ids"] for item in built],
            [["globex-prod", "globex-staging"], ["acme-prod"]],
            msg="ADR-002 keeps one route's environments out of another route's candidate",
        )
        self.assertEqual(
            len({item["candidate_id"] for item in built}),
            2,
            msg="route is a candidate identity input",
        )

    def test_unknown_service_is_rejected(self):
        config = copy.deepcopy(self.config)
        del config["services"]["eks"]
        announcement = NormalizedAnnouncement(
            canonical_url=self.source["url"],
            title=self.source["title"],
            summary=self.source["summary"],
            observed_at=parse_timestamp(self.source["observed_at"]),
            published_at=None,
            provenance=(Provenance(feed_name="aws-whats-new", item_url=self.source["url"]),),
        )
        match = next(
            result
            for result in match_announcement(
                Announcement(title=announcement.title, summary=announcement.summary),
                load_services(self.config),
                load_risk_rules(self.config),
            )
            if result.pair == ("eks", "service-version-update")
        )
        with self.assertRaises(ValueError):
            build_candidate(
                announcement=announcement,
                match=match,
                audience=route_audiences(self.config, self.inventory, "eks")[0],
                configuration=config,
                release=self.candidate["release"],
                created_at=parse_timestamp(self.candidate["created_at"]),
            )

    def test_unknown_feed_in_provenance_is_rejected(self):
        announcement = NormalizedAnnouncement(
            canonical_url=self.source["url"],
            title=self.source["title"],
            summary=self.source["summary"],
            observed_at=parse_timestamp(self.source["observed_at"]),
            published_at=None,
            provenance=(Provenance(feed_name="absent-feed", item_url=self.source["url"]),),
        )
        match = next(
            result
            for result in match_announcement(
                Announcement(title=announcement.title, summary=announcement.summary),
                load_services(self.config),
                load_risk_rules(self.config),
            )
            if result.pair == ("eks", "service-version-update")
        )
        with self.assertRaises(ValueError):
            build_candidate(
                announcement=announcement,
                match=match,
                audience=route_audiences(self.config, self.inventory, "eks")[0],
                configuration=self.config,
                release=self.candidate["release"],
                created_at=parse_timestamp(self.candidate["created_at"]),
            )


class DerivationTests(FixtureBase):
    def test_reason_matches_the_committed_prose(self):
        self.assertEqual(
            explainability_reason(
                self.candidate["service"]["display_name"],
                self.candidate["risk"]["risk_type"],
            ),
            self.candidate["explainability"]["reason"],
        )

    def test_naive_timestamps_are_rejected(self):
        with self.assertRaises(ValueError):
            utc_timestamp(datetime(2026, 7, 13, 17, 0, 0))

    def test_non_utc_timestamps_are_converted(self):
        moment = parse_timestamp(self.candidate["created_at"])
        shifted = moment.astimezone(timezone(timedelta(hours=-4)))
        self.assertEqual(utc_timestamp(shifted), self.candidate["created_at"])


if __name__ == "__main__":
    unittest.main()
