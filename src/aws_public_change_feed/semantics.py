"""Cross-document candidate rules, shared by the validator and the runtime.

Chapter 04 defines what makes an `AlertCandidate` correct, and correctness is
not a property of its shape. The schema says a candidate has a service display
name, a priority, an explanation, and a recommended action; only these rules
say those must be the ones the release actually specifies, and that the rule
cited really does fire on the announcement quoted.

This lived in `scripts/validate_config.py` and so ran over the committed
fixtures and nowhere else. The delivery worker validated a stored request
against its schema, verified the exact release the candidate embeds, and then
rendered the candidate's fields as given. A stored request edited in place —
preserving every identity digest and release hash, because none of them cover
these fields — therefore rendered and posted whatever it said: a forged
high-priority mention, a service display name from no release, and a
"recommended action" of the editor's choosing.

The identity digests cannot close that on their own. `candidate_id` covers
revision, service ID, risk type, route, and audience; `audience_fingerprint`
covers the environment set. Display name, priority, reason, and recommended
action are outside every digest by design, because they are projections of the
release rather than identity. Binding them needs the release, which is exactly
what these rules do.

`validate_candidate_against_release` holds the checks that need only a config
and an inventory. The manifest-dependent checks — that a candidate's release
block matches the active manifest, and that it was not created before the
promotion — stay in the validator, which is where a manifest exists; the
worker gets that guarantee from having loaded and hash-verified the exact
release the candidate names.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from .candidates import explainability_reason, utc_timestamp
from .identity import (
    announcement_id,
    audience_fingerprint,
    candidate_id,
    canonical_public_url,
    content_fingerprint,
    evidence_order,
    identity_text,
    revision_id,
)
from .profiles import route_audiences
from .urls import FeedUrlRejected, validate_feed_url, validate_source_item_url

__all__ = [
    "CandidateSemanticsError",
    "configured_feed_hosts",
    "parsed_timestamp",
    "phrase_spans",
    "serialized_size",
    "validate_candidate_against_release",
    "validate_host",
    "validate_public_url",
]


class CandidateSemanticsError(ValueError):
    """A candidate disagrees with the release it claims to have come from.

    Raised for a candidate whose bytes are well-formed and whose identities are
    intact, but whose content is not what the release specifies. In the
    validator this fails a build; in the worker it is a `failed_terminal`
    delivery, because no retry repairs a record that says the wrong thing.
    """


def serialized_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def parsed_timestamp(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("contract timestamps require an explicit UTC offset")
    return parsed


def validate_host(host: str, label: str) -> None:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError(f"{label} cannot use an IP literal: {host}")
    labels = host.split(".")
    if not labels or any(
        not value
        or len(value) > 63
        or not value[0].isalnum()
        or not value[-1].isalnum()
        or any(not character.isalnum() and character != "-" for character in value)
        for value in labels
    ):
        raise ValueError(f"{label} is not a valid DNS hostname: {host}")


def validate_public_url(raw_url: str, allowed_hosts: set[str], label: str) -> None:
    """Apply the public URL policy to a configured or recorded link."""

    try:
        validated = validate_feed_url(raw_url, allowed_hosts)
    except FeedUrlRejected as error:
        raise ValueError(f"{label} failed the canonical HTTPS policy: {error}") from error
    validate_host(validated.hostname, label)


def phrase_spans(text: str, phrase: str) -> list[tuple[int, int]]:
    normalized_content = identity_text(text)
    normalized_phrase = identity_text(phrase)
    pattern = re.compile(rf"(?<!\w){re.escape(normalized_phrase)}(?!\w)")
    return [(match.start(), match.end()) for match in pattern.finditer(normalized_content)]


def configured_feed_hosts(config: Mapping[str, Any]) -> set[str]:
    """The hosts the release's own feed list names.

    The cross-document validator proves this set equal to the deployment's
    `allowed_feed_hosts`: every configured feed host must be allowed, and every
    allowed host must be used by a configured feed. So this is not a tighter
    substitute for the deployment allowlist that the runtime cannot read — it
    is the same set, reached through the document the candidate embeds.
    """

    return {(urlsplit(feed["url"]).hostname or "").casefold() for feed in config["feeds"]}


def validate_candidate_against_release(
    config: Mapping[str, Any],
    inventory: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> None:
    """Refuse a candidate that disagrees with this config and inventory.

    Every rule chapter 04 states about a candidate's relationship to its
    release, minus the two that need a manifest. Recomputes the announcement,
    content, revision, audience, and candidate identities; re-derives the
    service and rule projections; re-evaluates the risk rule against the
    announcement text it quotes; re-derives the route audience; and checks
    provenance ordering and URLs.

    Raises `CandidateSemanticsError` on the first disagreement.
    """

    try:
        _validate(config, inventory, candidate)
    except CandidateSemanticsError:
        raise
    except (ValueError, KeyError, TypeError) as error:
        # A malformed candidate that reaches here is still a candidate that
        # disagrees with its release; callers distinguish one exception type.
        raise CandidateSemanticsError(str(error)) from error


def _validate(
    config: Mapping[str, Any],
    inventory: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> None:
    # The byte ceiling first: it is a property of the serialized document
    # rather than of its agreement with the release, it is the cheapest check
    # here, and running it before the rule re-evaluation means an oversize
    # candidate is reported as oversize rather than as whichever derived field
    # its padding happened to land in.
    policy = config["message_policy"]
    if serialized_size(candidate) > policy["max_candidate_bytes"]:
        raise CandidateSemanticsError(f"alert candidate exceeds {policy['max_candidate_bytes']} UTF-8 JSON bytes")

    announcement = candidate["announcement"]
    feed_hosts = configured_feed_hosts(config)
    validate_public_url(announcement["url"], feed_hosts, "announcement URL")

    expected_announcement_id = announcement_id(canonical_public_url(announcement["url"]))
    if announcement["announcement_id"] != expected_announcement_id:
        raise CandidateSemanticsError("announcement_id differs from the canonical announcement URL")
    expected_fingerprint = content_fingerprint(announcement["title"], announcement["summary"])
    if announcement["content_fingerprint"] != expected_fingerprint:
        raise CandidateSemanticsError("content_fingerprint differs from normalized announcement content")
    expected_revision_id = revision_id(announcement["announcement_id"], announcement["content_fingerprint"])
    if announcement["revision_id"] != expected_revision_id:
        raise CandidateSemanticsError("revision_id differs from announcement identity and content")
    expected_audience_fingerprint = audience_fingerprint(candidate["environment_ids"])
    if candidate["audience_fingerprint"] != expected_audience_fingerprint:
        raise CandidateSemanticsError("audience_fingerprint differs from sorted candidate environment IDs")
    expected_candidate_id = candidate_id(
        announcement["revision_id"],
        candidate["service"]["id"],
        candidate["risk"]["risk_type"],
        candidate["route_id"],
        candidate["audience_fingerprint"],
    )
    if candidate["candidate_id"] != expected_candidate_id:
        raise CandidateSemanticsError("candidate_id differs from its canonical identity fields")
    timestamp_fields = {
        "candidate created_at": candidate["created_at"],
        "announcement observed_at": announcement["observed_at"],
    }
    if "published_at" in announcement:
        timestamp_fields["announcement published_at"] = announcement["published_at"]
    parsed_times = {}
    for label, value in timestamp_fields.items():
        parsed = parsed_timestamp(value)
        if utc_timestamp(parsed) != value:
            raise CandidateSemanticsError(f"{label} is not a normalized UTC timestamp")
        parsed_times[label] = parsed
    if parsed_times["candidate created_at"] < parsed_times["announcement observed_at"]:
        raise CandidateSemanticsError("candidate creation predates announcement observation")

    service_id = candidate["service"]["id"]
    service = config["services"].get(service_id)
    if not service:
        raise CandidateSemanticsError(f"candidate references unknown service: {service_id}")
    if candidate["service"]["display_name"] != service["display_name"]:
        raise CandidateSemanticsError("candidate service display name differs from config")
    if candidate["recommended_action"] != service["recommended_action"]:
        raise CandidateSemanticsError("candidate recommended action differs from config")

    rules = {rule["id"]: rule for rule in config["risk_rules"]}
    rule = rules.get(candidate["risk"]["rule_id"])
    if not rule:
        raise CandidateSemanticsError("candidate references unknown risk rule")
    for field in ("risk_type", "priority"):
        if candidate["risk"][field] != rule[field]:
            raise CandidateSemanticsError(f"candidate risk {field} differs from config")

    fields = {field: announcement[field] for field in rule["fields"]}
    matched_alias_values = sorted(
        (alias for alias in service["aliases"] if any(phrase_spans(text, alias) for text in fields.values())),
        key=evidence_order,
    )
    if candidate["explainability"]["matched_aliases"] != matched_alias_values:
        raise CandidateSemanticsError("candidate matched aliases differ from announcement service evidence")

    term_matches = {
        group: {
            term: {field: phrase_spans(text, term) for field, text in fields.items()} for term in rule["match"][group]
        }
        for group in ("any", "all", "none")
    }
    present_any = [term for term, locations in term_matches["any"].items() if any(locations.values())]
    present_all = [term for term, locations in term_matches["all"].items() if any(locations.values())]
    present_none = [term for term, locations in term_matches["none"].items() if any(locations.values())]
    if (rule["match"]["any"] and not present_any) or len(present_all) != len(rule["match"]["all"]) or present_none:
        raise CandidateSemanticsError("candidate announcement does not satisfy its risk rule")
    matched_term_values = sorted(present_any + present_all, key=evidence_order)
    if candidate["explainability"]["matched_terms"] != matched_term_values:
        raise CandidateSemanticsError("candidate matched terms differ from announcement risk evidence")

    evidence_fields = []
    distinct_evidence = False
    for field in rule["fields"]:
        alias_spans = [span for alias in matched_alias_values for span in phrase_spans(fields[field], alias)]
        risk_spans = [span for term in matched_term_values for span in phrase_spans(fields[field], term)]
        if alias_spans or risk_spans:
            evidence_fields.append(field)
        if any(
            alias_end <= risk_start or risk_end <= alias_start
            for alias_start, alias_end in alias_spans
            for risk_start, risk_end in risk_spans
        ):
            distinct_evidence = True
    if not distinct_evidence:
        alias_fields = {
            field
            for field in rule["fields"]
            if any(phrase_spans(fields[field], value) for value in matched_alias_values)
        }
        risk_fields = {
            field
            for field in rule["fields"]
            if any(phrase_spans(fields[field], value) for value in matched_term_values)
        }
        distinct_evidence = bool(alias_fields and risk_fields and alias_fields != risk_fields)
    if not distinct_evidence:
        raise CandidateSemanticsError("candidate service and risk evidence must be distinct")
    if candidate["explainability"]["matched_fields"] != evidence_fields:
        raise CandidateSemanticsError("candidate matched fields differ from announcement evidence")

    # `reason` is the sentence the reader is shown as the explanation, and it
    # is a deterministic template over the service display name and risk type,
    # so it is derivable and therefore checkable. Nothing checked it before —
    # not here and not in the validator this came from — which left the one
    # free-text field the message presents as evidence unbound to the release.
    expected_reason = explainability_reason(service["display_name"], rule["risk_type"])
    if candidate["explainability"]["reason"] != expected_reason:
        raise CandidateSemanticsError("candidate explanation differs from the release-derived reason")

    if candidate["route_id"] not in inventory["slack"]["routes"]:
        raise CandidateSemanticsError("candidate references unknown route")
    # The mapping rule lives in the runtime, for the reason identity.py gives:
    # a validator with its own copy can agree with the fixture while disagreeing
    # with the code that builds candidates, and the fixture still passes.
    audience = next(
        (entry for entry in route_audiences(config, inventory, service_id) if entry.route_id == candidate["route_id"]),
        None,
    )
    expected_environment_ids = list(audience.environment_ids) if audience else []
    expected_profile_ids = list(audience.profile_ids) if audience else []
    if candidate["environment_ids"] != expected_environment_ids:
        raise CandidateSemanticsError("candidate environment IDs differ from the complete route and profile mapping")
    if candidate["explainability"]["matched_profile_ids"] != expected_profile_ids:
        raise CandidateSemanticsError("candidate matched profile IDs differ from the complete environment mapping")

    feed_urls = {feed["name"]: canonical_public_url(feed["url"]) for feed in config["feeds"]}
    provenance_order = [(item["feed_name"], item["source_item_id"]) for item in announcement["provenance"]]
    if provenance_order != sorted(provenance_order):
        raise CandidateSemanticsError("announcement provenance must use stable lexical order")
    for item in announcement["provenance"]:
        expected_url = feed_urls.get(item["feed_name"])
        if expected_url is None:
            raise CandidateSemanticsError(f"announcement provenance names an unknown feed: {item['feed_name']}")
        if canonical_public_url(item["feed_url"]) != expected_url:
            raise CandidateSemanticsError(f"announcement provenance URL differs from config: {item['feed_name']}")
        if item.get("source_item_url"):
            canonical_item_url = validate_source_item_url(item["source_item_url"], feed_hosts)
            if canonical_item_url != canonical_public_url(announcement["url"]):
                raise CandidateSemanticsError(
                    "announcement provenance item URL differs from the canonical announcement URL"
                )
