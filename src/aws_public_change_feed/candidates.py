"""AlertCandidate version 3 construction.

Chapter 04 "AlertCandidate version 3" lists the contract fields and ADR-002
fixes the identity. This module turns one `MatchResult` and one `RouteAudience`
into the document the outbox persists and the delivery request embeds.

The schema is authoritative for shape. What this module owns is the derivation:
which values come from the match, which from the release, and which are computed
so that the same announcement revision cannot produce two different candidates.

`examples/alert-candidate.json` is the oracle. `tests/test_candidates.py` rebuilds
it from `examples/config.yaml` and `examples/inventory.json` and requires the
result to equal the committed document, so the runtime is bound to the contract
rather than to itself.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .announcements import NormalizedAnnouncement
from .identity import candidate_id, identity_text
from .matching import MatchResult
from .profiles import RouteAudience

__all__ = [
    "CONTRACT_VERSION",
    "RELEVANCE_BASIS",
    "build_candidate",
    "build_candidates",
    "explainability_reason",
    "utc_timestamp",
]

CONTRACT_VERSION = 3

# ADR-017 and chapter 04 permit exactly one basis. A public announcement never
# proves customer impact, so the wording stays "potentially relevant" wherever
# this value is rendered.
RELEVANCE_BASIS = "inferred_profile_match"


def utc_timestamp(moment: datetime) -> str:
    """Render an aware datetime as an RFC 3339 timestamp with a `Z` offset.

    Naive datetimes are rejected rather than assumed to be UTC. A candidate
    carries `created_at` and `observed_at` into durable state, and a silently
    mislabelled local time is not recoverable once written.
    """

    if moment.tzinfo is None:
        raise ValueError("candidate timestamps require an aware datetime")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def explainability_reason(service_display_name: str, risk_type: str) -> str:
    """The human-readable reason shown with the candidate.

    Deliberately a template over the values that already appear in the
    candidate, so the sentence cannot claim evidence the contract does not
    carry. Everything a reviewer needs to check it is in `explainability`.
    """

    return (
        f"The announcement matched the {service_display_name} service definition and the configured {risk_type} rule."
    )


def _provenance(
    announcement: NormalizedAnnouncement,
    feed_urls: Mapping[str, str],
) -> list[dict[str, str]]:
    """Build sorted candidate provenance from the announcement's sightings.

    `source_item_id` comes from the item URL because that is the item identity
    the acquisition path currently retains: `Provenance` carries `feed_name` and
    `item_url` and no separate feed GUID. If feeds later supply a distinct ID,
    `Provenance` gains a field and this derivation changes with it.
    """

    entries = []
    for sighting in announcement.provenance:
        feed_url = feed_urls.get(sighting.feed_name)
        if feed_url is None:
            raise ValueError(f"announcement provenance names an unknown feed: {sighting.feed_name}")
        entries.append(
            {
                "feed_name": sighting.feed_name,
                "feed_url": feed_url,
                "source_item_id": sighting.item_url,
                "source_item_url": sighting.item_url,
            }
        )
    # Chapter 04 requires stable lexical order, and the validator rejects any
    # other. Sorting here rather than trusting `coalesce` keeps a hand-built
    # announcement from producing a candidate the validator would reject.
    entries.sort(key=lambda entry: (entry["feed_name"], entry["source_item_id"]))
    return entries


def build_candidate(
    *,
    announcement: NormalizedAnnouncement,
    match: MatchResult,
    audience: RouteAudience,
    configuration: Mapping[str, Any],
    release: Mapping[str, Any],
    created_at: datetime,
    is_update: bool = False,
) -> dict[str, Any]:
    """Build one route-scoped candidate.

    One call produces the candidate for one route. `route_audiences` returns the
    routes, and the caller builds one candidate per entry.
    """

    service = configuration["services"].get(match.service_id)
    if service is None:
        raise ValueError(f"match names a service absent from the release: {match.service_id}")

    feed_urls = {feed["name"]: feed["url"] for feed in configuration["feeds"]}
    fingerprint = audience.audience_fingerprint

    candidate: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "candidate_id": candidate_id(
            announcement.revision_id,
            match.service_id,
            match.risk_type,
            audience.route_id,
            fingerprint,
        ),
        "audience_fingerprint": fingerprint,
        "created_at": utc_timestamp(created_at),
        "announcement": {
            "source_type": announcement.source_type,
            "announcement_id": announcement.announcement_id,
            "revision_id": announcement.revision_id,
            "content_fingerprint": announcement.content_fingerprint,
            "title": announcement.title,
            "summary": announcement.summary,
            "url": announcement.canonical_url,
            "observed_at": utc_timestamp(announcement.observed_at),
            "is_update": is_update,
            "provenance": _provenance(announcement, feed_urls),
        },
        "service": {
            "id": match.service_id,
            "display_name": match.service_display_name,
        },
        "risk": {
            "rule_id": match.rule_id,
            "risk_type": match.risk_type,
            "priority": match.priority,
        },
        "route_id": audience.route_id,
        "environment_ids": list(audience.environment_ids),
        "relevance_basis": RELEVANCE_BASIS,
        "explainability": {
            "matched_profile_ids": list(audience.profile_ids),
            # Chapter 04 fixes normalized lexical order for aliases and terms and
            # configured order for fields, so only the first two are sorted.
            "matched_aliases": sorted(match.matched_aliases, key=identity_text),
            "matched_terms": sorted(match.matched_terms, key=identity_text),
            "matched_fields": list(match.matched_fields),
            "reason": explainability_reason(match.service_display_name, match.risk_type),
        },
        "recommended_action": service["recommended_action"],
        "release": copy.deepcopy(dict(release)),
    }

    published_at = announcement.published_at
    if published_at is not None:
        # Chapter 04 makes publication time optional. The key is omitted rather
        # than nulled, because the schema forbids unknown and null-typed fields.
        candidate["announcement"]["published_at"] = utc_timestamp(published_at)

    return candidate


def build_candidates(
    *,
    announcement: NormalizedAnnouncement,
    match: MatchResult,
    audiences: Sequence[RouteAudience],
    configuration: Mapping[str, Any],
    release: Mapping[str, Any],
    created_at: datetime,
    is_update: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Build one candidate per route audience, in the order given."""

    return tuple(
        build_candidate(
            announcement=announcement,
            match=match,
            audience=audience,
            configuration=configuration,
            release=release,
            created_at=created_at,
            is_update=is_update,
        )
        for audience in audiences
    )
