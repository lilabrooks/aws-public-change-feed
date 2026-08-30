"""Preview and apply one exact retained-response replay."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from .announcements import NormalizedAnnouncement, coalesce, normalize_item
from .candidates import build_candidates, utc_timestamp
from .dispatch import validate_alert_candidate
from .feedparse import FeedParseRejected, parse_feed
from .loading import LoadedRelease
from .matching import Announcement, load_risk_rules, load_services, match_announcement
from .outbox import TTL_ELIGIBLE_STATES, DeliveryRecord, EmissionResult, OutboxStore, emit, verify_durable
from .profiles import route_audiences
from .semantics import serialized_size, validate_candidate_against_release
from .state import AnnouncementRecord, AnnouncementStateStore, ResponsePageMarker, observe, record_emission
from .watcher import response_page_set_id, response_run_id

__all__ = [
    "ReplayRefused",
    "RetainedSnapshot",
    "apply_source_replay",
    "canonical_json",
    "create_source_replay_plan",
    "sha256_bytes",
]

_PAGE_SIZE = 25
_MAX_RETENTION_DAYS = 3650


class ReplayRefused(RuntimeError):
    """The retained response or saved replay plan cannot be used safely."""

    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RetainedSnapshot:
    key: str
    feed_name: str
    observed_at: datetime
    body: bytes
    body_sha256: str

    def __post_init__(self) -> None:
        if not self.key or not self.feed_name:
            raise ValueError("snapshot key and feed name must be nonempty")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("snapshot observed_at must include a UTC offset")
        if not isinstance(self.body, bytes):
            raise ValueError("snapshot body must be bytes")
        if hashlib.sha256(self.body).hexdigest() != self.body_sha256:
            raise ValueError("snapshot body digest does not match its bytes")


def canonical_json(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _retention_days(release: LoadedRelease, name: str) -> int:
    policy = release.config.get("state_retention")
    value = policy.get(name) if isinstance(policy, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_RETENTION_DAYS:
        raise ReplayRefused("invalid_release", f"release {name} is malformed")
    return value


def _feed_hosts(release: LoadedRelease) -> tuple[str, ...]:
    feeds = release.config.get("feeds")
    if not isinstance(feeds, list):
        raise ReplayRefused("invalid_release", "release feeds are malformed")
    hosts: set[str] = set()
    names: set[str] = set()
    for feed in feeds:
        if (
            not isinstance(feed, Mapping)
            or not isinstance(feed.get("name"), str)
            or not isinstance(feed.get("url"), str)
        ):
            raise ReplayRefused("invalid_release", "release feeds are malformed")
        names.add(feed["name"])
        host = (urlsplit(feed["url"]).hostname or "").casefold()
        if not host:
            raise ReplayRefused("invalid_release", "release feed URL has no host")
        hosts.add(host)
    return tuple(sorted(hosts)) if names else ()


def _configured_feed_names(release: LoadedRelease) -> frozenset[str]:
    feeds = release.config.get("feeds")
    if not isinstance(feeds, list):
        raise ReplayRefused("invalid_release", "release feeds are malformed")
    return frozenset(str(feed.get("name")) for feed in feeds if isinstance(feed, Mapping))


def _record_document(record: object | None) -> object:
    if isinstance(record, (AnnouncementRecord, ResponsePageMarker, DeliveryRecord)):
        return asdict(record)
    return record


class _ShadowAnnouncementStore:
    def __init__(self, durable: AnnouncementStateStore) -> None:
        self.durable = durable
        self.records: dict[str, AnnouncementRecord] = {}
        self.pages: dict[tuple[str, str, int], ResponsePageMarker] = {}
        self.initial_records: dict[str, AnnouncementRecord | None] = {}
        self.initial_pages: dict[tuple[str, str, int], ResponsePageMarker | None] = {}

    def load(self, announcement: str) -> AnnouncementRecord | None:
        if announcement in self.records:
            return self.records[announcement]
        if announcement not in self.initial_records:
            self.initial_records[announcement] = self.durable.load(announcement)
        return self.initial_records[announcement]

    def save(self, record: AnnouncementRecord) -> None:
        self.records[record.announcement_id] = record

    def put(self, record: AnnouncementRecord, *, expected_state_version: int | None) -> bool:
        existing = self.load(record.announcement_id)
        if expected_state_version is None:
            if existing is not None or record.state_version != 1:
                return False
        elif existing is None or existing.state_version != expected_state_version:
            return False
        self.records[record.announcement_id] = record
        return True

    def load_page(self, run_id: str, page_set_id: str, page: int) -> ResponsePageMarker | None:
        key = (run_id, page_set_id, page)
        if key in self.pages:
            return self.pages[key]
        if key not in self.initial_pages:
            self.initial_pages[key] = self.durable.load_page(*key)
        return self.initial_pages[key]

    def put_page(self, marker: ResponsePageMarker) -> bool:
        key = (marker.run_id, marker.page_set_id, marker.page)
        existing = self.load_page(*key)
        if existing is not None and existing != marker:
            return False
        if existing is not None and existing.expires_at is not None and marker.expires_at is not None:
            marker = ResponsePageMarker(
                **{**asdict(existing), "expires_at": max(existing.expires_at, marker.expires_at)}
            )
        self.pages[key] = marker
        return True


class _ShadowOutboxStore:
    def __init__(self, durable: OutboxStore) -> None:
        self.durable = durable
        self.candidates: dict[str, Mapping[str, Any]] = {}
        self.deliveries: dict[str, DeliveryRecord] = {}
        self.initial_candidates: dict[str, Mapping[str, Any] | None] = {}
        self.initial_deliveries: dict[str, DeliveryRecord | None] = {}

    def get_candidate(self, candidate: str) -> Mapping[str, Any] | None:
        if candidate in self.candidates:
            return self.candidates[candidate]
        if candidate not in self.initial_candidates:
            self.initial_candidates[candidate] = self.durable.get_candidate(candidate)
        return self.initial_candidates[candidate]

    def put_candidate_if_absent(self, candidate: Mapping[str, Any]) -> bool:
        key = str(candidate["candidate_id"])
        if self.get_candidate(key) is not None:
            return False
        self.candidates[key] = json.loads(json.dumps(candidate))
        return True

    def get_delivery(self, candidate: str) -> DeliveryRecord | None:
        if candidate in self.deliveries:
            return self.deliveries[candidate]
        if candidate not in self.initial_deliveries:
            self.initial_deliveries[candidate] = self.durable.get_delivery(candidate)
        return self.initial_deliveries[candidate]

    def put_delivery_if_absent(self, record: DeliveryRecord, *, now: int | None = None) -> bool:
        existing = self.get_delivery(record.candidate_id)
        timestamp = int(time.time()) if now is None else now
        if existing is not None and (
            existing.expires_at is None
            or existing.expires_at >= timestamp
            or existing.status not in TTL_ELIGIBLE_STATES
        ):
            return False
        self.deliveries[record.candidate_id] = record
        return True


@dataclass(frozen=True, slots=True)
class _Evaluation:
    announcements: tuple[NormalizedAnnouncement, ...]
    candidate_ids: tuple[str, ...]
    route_ids: tuple[str, ...]
    emission: EmissionResult
    run_id: str
    page_set_id: str
    page_count: int


def _evaluate(
    snapshot: RetainedSnapshot,
    release: LoadedRelease,
    *,
    application_version: str,
    max_items: int,
    max_item_characters: int,
    created_at: datetime,
    expected_route_ids: Sequence[str],
    announcement_state: AnnouncementStateStore,
    outbox: OutboxStore,
) -> _Evaluation:
    if snapshot.feed_name not in _configured_feed_names(release):
        raise ReplayRefused("invalid_release", "snapshot feed is absent from the exact release")
    hosts = _feed_hosts(release)
    try:
        items = parse_feed(snapshot.body, max_items=max_items, max_item_characters=max_item_characters)
    except FeedParseRejected as error:
        raise ReplayRefused(
            "invalid_snapshot", f"retained response failed parser policy: {error.reason_class}"
        ) from error
    normalized = tuple(
        item
        for parsed in items
        if (item := normalize_item(parsed, snapshot.feed_name, snapshot.observed_at, hosts)) is not None
    )
    announcements = coalesce(normalized)
    services = load_services(release.config)
    rules = load_risk_rules(release.config)
    retention_days = _retention_days(release, "announcement_state_ttl_days")
    page_retention_days = _retention_days(release, "feed_state_ttl_days")
    built: list[Mapping[str, Any]] = []
    by_announcement: dict[str, list[str]] = {}

    for announcement in announcements:
        observation = observe(announcement_state, announcement, retention_days=retention_days)
        matches = match_announcement(Announcement(announcement.title, announcement.summary), services, rules)
        for match in matches:
            for candidate in build_candidates(
                announcement=announcement,
                match=match,
                audiences=route_audiences(release.config, release.inventory, match.service_id),
                configuration=release.config,
                release=release.reference,
                created_at=created_at,
                is_update=observation.is_update,
            ):
                if serialized_size(candidate) > release.config["message_policy"]["max_candidate_bytes"]:
                    raise ReplayRefused("invalid_release", "replay candidate exceeds the release byte limit")
                validate_alert_candidate(candidate)
                validate_candidate_against_release(release.config, release.inventory, candidate)
                built.append(candidate)
                by_announcement.setdefault(announcement.announcement_id, []).append(str(candidate["candidate_id"]))

    route_ids = tuple(sorted({str(candidate["route_id"]) for candidate in built}))
    expected = tuple(sorted(set(expected_route_ids)))
    if route_ids != expected:
        raise ReplayRefused(
            "route_scope_mismatch",
            f"replay routes {list(route_ids)!r} differ from expected routes {list(expected)!r}",
        )
    for built_candidate in built:
        key = str(built_candidate["candidate_id"])
        stored = outbox.get_candidate(key)
        delivery = outbox.get_delivery(key)
        if stored is None or delivery is not None:
            continue
        stored_release = stored.get("release")
        if not isinstance(stored_release, Mapping) or stored_release.get("release_id") != release.release_id:
            raise ReplayRefused(
                "release_mismatch",
                "a missing delivery cannot be repaired because its stored candidate names another release",
            )
    emission = emit(
        outbox,
        built,
        inventory=release.inventory,
        message_policy=release.config["message_policy"],
        created_at=created_at,
    )
    if not verify_durable(outbox, emission.candidate_ids):
        raise ReplayRefused("durability_failed", "replay outbox read-back failed")
    for announcement_id, announcement_candidate_ids in by_announcement.items():
        record_emission(announcement_state, announcement_id, announcement_candidate_ids, release.release_id)

    candidate_ids: tuple[str, ...] = tuple(sorted(set(emission.candidate_ids)))
    run_id = response_run_id(snapshot.feed_name, snapshot.body_sha256, release.release_id, application_version)
    page_set_id = response_page_set_id(run_id, candidate_ids)
    pages: list[tuple[str, ...]] = [
        candidate_ids[index : index + _PAGE_SIZE] for index in range(0, len(candidate_ids), _PAGE_SIZE)
    ] or [()]
    expires_at = int((created_at + timedelta(days=page_retention_days)).timestamp())
    for number, candidate_page in enumerate(pages):
        marker = ResponsePageMarker(
            run_id=run_id,
            page_set_id=page_set_id,
            feed_name=snapshot.feed_name,
            page=number,
            candidate_ids=candidate_page,
            expires_at=expires_at,
        )
        if not announcement_state.put_page(marker):
            raise ReplayRefused("state_conflict", "response-page marker conflicts with durable state")
        durable = announcement_state.load_page(run_id, page_set_id, number)
        if durable is None or durable != marker or durable.expires_at is None or durable.expires_at < expires_at:
            raise ReplayRefused("durability_failed", "response-page marker read-back failed")
    return _Evaluation(
        announcements=announcements,
        candidate_ids=candidate_ids,
        route_ids=route_ids,
        emission=emission,
        run_id=run_id,
        page_set_id=page_set_id,
        page_count=len(pages),
    )


def _state_digest(announcements: _ShadowAnnouncementStore, outbox: _ShadowOutboxStore) -> str:
    document = {
        "announcements": {key: _record_document(value) for key, value in sorted(announcements.initial_records.items())},
        "pages": {
            f"{run_id}:{page_set_id}:{page}": _record_document(value)
            for (run_id, page_set_id, page), value in sorted(announcements.initial_pages.items())
        },
        "candidates": {key: value for key, value in sorted(outbox.initial_candidates.items())},
        "deliveries": {key: _record_document(value) for key, value in sorted(outbox.initial_deliveries.items())},
    }
    return sha256_bytes(canonical_json(document))


def create_source_replay_plan(
    snapshot: RetainedSnapshot,
    release: LoadedRelease,
    *,
    pointer_key: str,
    pointer_version_id: str,
    application_version: str,
    max_items: int,
    max_item_characters: int,
    planned_at: datetime,
    operator: str,
    purpose: str,
    expected_route_ids: Sequence[str],
    announcement_state: AnnouncementStateStore,
    outbox: OutboxStore,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a read-only replay plan against strongly read durable state."""

    if planned_at.tzinfo is None or planned_at.utcoffset() is None:
        raise ValueError("planned_at must include a UTC offset")
    for name, value, maximum in (("operator", operator, 200), ("purpose", purpose, 1000)):
        if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
            raise ValueError(f"{name} must be a bounded nonempty string without surrounding whitespace")
    shadow_announcements = _ShadowAnnouncementStore(announcement_state)
    shadow_outbox = _ShadowOutboxStore(outbox)
    evaluation = _evaluate(
        snapshot,
        release,
        application_version=application_version,
        max_items=max_items,
        max_item_characters=max_item_characters,
        created_at=planned_at,
        expected_route_ids=expected_route_ids,
        announcement_state=shadow_announcements,
        outbox=shadow_outbox,  # type: ignore[arg-type]
    )
    existing_candidates = tuple(
        key for key in evaluation.candidate_ids if shadow_outbox.initial_candidates.get(key) is not None
    )
    existing_deliveries = tuple(
        key for key in evaluation.candidate_ids if shadow_outbox.initial_deliveries.get(key) is not None
    )
    existing_pages = sum(value is not None for value in shadow_announcements.initial_pages.values())
    return {
        "schema_version": 1,
        "action": "source_replay",
        "planned_at": utc_timestamp(planned_at),
        "operator": operator,
        "purpose": purpose,
        "snapshot": {
            "key": snapshot.key,
            "feed_name": snapshot.feed_name,
            "observed_at": snapshot.observed_at.astimezone(UTC).isoformat(),
            "body_sha256": snapshot.body_sha256,
            "byte_count": len(snapshot.body),
        },
        "release": {
            "pointer_key": pointer_key,
            "pointer_version_id": pointer_version_id,
            "release_id": release.release_id,
            "config": dict(release.reference["config"]),
            "inventory": dict(release.reference["inventory"]),
            "application_version": application_version,
        },
        "limits": {"max_items": max_items, "max_item_characters": max_item_characters},
        "expected_route_ids": list(sorted(set(expected_route_ids))),
        "result": {
            "announcement_ids": sorted(item.announcement_id for item in evaluation.announcements),
            "candidate_ids": list(evaluation.candidate_ids),
            "route_ids": list(evaluation.route_ids),
            "existing_candidate_ids": list(existing_candidates),
            "missing_candidate_ids": sorted(set(evaluation.candidate_ids) - set(existing_candidates)),
            "existing_delivery_ids": list(existing_deliveries),
            "missing_delivery_ids": sorted(set(evaluation.candidate_ids) - set(existing_deliveries)),
            "run_id": evaluation.run_id,
            "page_set_id": evaluation.page_set_id,
            "existing_page_count": existing_pages,
            "missing_page_count": evaluation.page_count - existing_pages,
        },
        "state_sha256": _state_digest(shadow_announcements, shadow_outbox),
        "context": {} if context is None else dict(context),
    }


def apply_source_replay(
    snapshot: RetainedSnapshot,
    release: LoadedRelease,
    plan: Mapping[str, Any],
    *,
    pointer_key: str,
    pointer_version_id: str,
    application_version: str,
    max_items: int,
    max_item_characters: int,
    announcement_state: AnnouncementStateStore,
    outbox: OutboxStore,
    context: Mapping[str, Any] | None = None,
    before_write: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Rebuild an unchanged preview, then fill its missing durable state."""

    try:
        planned_at = datetime.fromisoformat(str(plan["planned_at"]).replace("Z", "+00:00"))
        operator = str(plan["operator"])
        purpose = str(plan["purpose"])
        expected_routes = plan["expected_route_ids"]
    except (KeyError, TypeError, ValueError) as error:
        raise ReplayRefused("stale_plan", "saved replay plan is malformed") from error
    if not isinstance(expected_routes, list) or not all(isinstance(value, str) for value in expected_routes):
        raise ReplayRefused("stale_plan", "saved replay route scope is malformed")
    current = create_source_replay_plan(
        snapshot,
        release,
        pointer_key=pointer_key,
        pointer_version_id=pointer_version_id,
        application_version=application_version,
        max_items=max_items,
        max_item_characters=max_item_characters,
        planned_at=planned_at,
        operator=operator,
        purpose=purpose,
        expected_route_ids=expected_routes,
        announcement_state=announcement_state,
        outbox=outbox,
        context=context,
    )
    if canonical_json(current) != canonical_json(plan):
        raise ReplayRefused("stale_plan", "snapshot, release, limits, or durable state differs from the preview")
    if before_write is not None:
        before_write()
    evaluation = _evaluate(
        snapshot,
        release,
        application_version=application_version,
        max_items=max_items,
        max_item_characters=max_item_characters,
        created_at=planned_at,
        expected_route_ids=expected_routes,
        announcement_state=announcement_state,
        outbox=outbox,
    )
    return {
        "status": "applied",
        "candidate_ids": list(evaluation.candidate_ids),
        "created_candidate_ids": list(evaluation.emission.created_candidates),
        "reused_candidate_ids": list(evaluation.emission.reused_candidates),
        "created_delivery_ids": list(evaluation.emission.created_deliveries),
        "repaired_delivery_ids": list(evaluation.emission.repaired_deliveries),
        "run_id": evaluation.run_id,
        "page_set_id": evaluation.page_set_id,
    }
