"""Direct-invocation, read-only evaluation through the deployed watcher path."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from .acquisition import load_feeds
from .fetching import FeedFetcher
from .identity import application_artifact_id
from .loading import IncompatibleRelease, LoadedRelease, ReleaseIntegrityError, load_active_release
from .outbox import InMemoryOutboxStore
from .releases import ObjectMissing, ObjectStore, S3ObjectStore
from .runtime_environment import (
    approved_hosts_from_environment,
    positive_environment_integer,
    required_environment,
    valid_invocation_id,
    zero_redirect_policy,
)
from .state import InMemoryAnnouncementStateStore, InMemoryFeedStateStore, InMemorySnapshotStore
from .watcher import WatcherOrchestrator, WatcherResult

_DIGEST = re.compile(r"[a-f0-9]{64}")
_EXPECTED_EVENT_FIELDS = frozenset(
    {
        "operation",
        "expected_release_id",
        "expected_application_version",
        "expected_feed_names",
    }
)


class _ShadowOrchestrator(Protocol):
    def run(
        self,
        release: LoadedRelease,
        *,
        invocation_id: str,
        remaining_time_ms: Callable[[], int],
    ) -> WatcherResult: ...


class _ShadowRefusal(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ShadowRuntime:
    """Load one exact active release and evaluate it only in ephemeral stores."""

    release_store: ObjectStore
    pointer_key: str
    application_version: str
    orchestrator: _ShadowOrchestrator
    outbox: InMemoryOutboxStore

    def run(
        self,
        *,
        invocation_id: str,
        remaining_time_ms: Callable[[], int],
        expected_release_id: str,
        expected_application_version: str,
        expected_feed_names: Sequence[str],
    ) -> dict[str, object]:
        release = load_active_release(
            self.release_store,
            pointer_key=self.pointer_key,
            application_version=self.application_version,
        )
        if release.release_id != expected_release_id:
            raise _ShadowRefusal("expected_release_mismatch")
        if self.application_version != expected_application_version:
            raise _ShadowRefusal("expected_application_mismatch")

        configured_feeds = tuple(sorted(feed.name for feed in load_feeds(release.config)))
        if configured_feeds != tuple(expected_feed_names):
            raise _ShadowRefusal("expected_feed_set_mismatch")

        result = self.orchestrator.run(
            release,
            invocation_id=invocation_id,
            remaining_time_ms=remaining_time_ms,
        )
        if result.incomplete:
            raise _ShadowRefusal("incomplete")

        outcome_feeds = tuple(sorted(outcome.feed_name for outcome in result.outcomes))
        if outcome_feeds != tuple(expected_feed_names):
            raise _ShadowRefusal("outcome_feed_set_mismatch")

        candidate_ids = tuple(sorted(result.candidate_ids))
        routes = tuple(
            sorted(
                {
                    str(candidate["route_id"])
                    for candidate_id in candidate_ids
                    if (candidate := self.outbox.get_candidate(candidate_id)) is not None
                }
            )
        )
        evidence_digest = hashlib.sha256(
            json.dumps(candidate_ids, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        outcomes = [
            {
                "feed_name": outcome.feed_name,
                "status": outcome.status,
                "error_class": outcome.error_class,
                "item_count": outcome.item_count,
                "candidate_count": len(outcome.candidate_ids),
            }
            for outcome in result.outcomes
        ]
        return {
            "classification": "passed"
            if all(item["status"] in ("fetched", "not_modified") for item in outcomes)
            else "failed",
            "release_id": release.release_id,
            "application_version": self.application_version,
            "invocation_id": invocation_id,
            "feed_count": len(outcomes),
            "normalized_item_count": sum(outcome.item_count for outcome in result.outcomes),
            "candidate_count": len(candidate_ids),
            "candidate_ids_sha256": evidence_digest,
            "route_ids": list(routes),
            "outcomes": outcomes,
        }


@dataclass(frozen=True, slots=True)
class _Configuration:
    config_bucket: str
    pointer_key: str
    application_version: str
    approved_hosts: tuple[str, ...]
    connect_timeout_seconds: int
    response_timeout_seconds: int
    max_response_bytes: int
    max_items: int
    max_item_characters: int
    max_concurrent_fetches: int
    lease_seconds: int


def _configuration_from_environment() -> _Configuration:
    zero_redirect_policy()
    return _Configuration(
        config_bucket=required_environment("CONFIG_BUCKET"),
        pointer_key=required_environment("ACTIVE_VERSIONS_OBJECT_KEY"),
        application_version=application_artifact_id(required_environment("APPLICATION_VERSION")),
        approved_hosts=approved_hosts_from_environment(),
        connect_timeout_seconds=positive_environment_integer("FEED_CONNECT_TIMEOUT_SECONDS"),
        response_timeout_seconds=positive_environment_integer("FEED_RESPONSE_TIMEOUT_SECONDS"),
        max_response_bytes=positive_environment_integer("MAX_FEED_RESPONSE_BYTES"),
        max_items=positive_environment_integer("MAX_FEED_ITEMS"),
        max_item_characters=positive_environment_integer("MAX_FEED_ITEM_CHARACTERS"),
        max_concurrent_fetches=positive_environment_integer("MAX_CONCURRENT_FETCHES"),
        lease_seconds=positive_environment_integer("FEED_LEASE_SECONDS"),
    )


def _event(event: Mapping[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    if not isinstance(event, Mapping) or set(event) != _EXPECTED_EVENT_FIELDS:
        raise ValueError("invalid shadow evaluation event")
    if event.get("operation") != "shadow_evaluate":
        raise ValueError("invalid shadow evaluation event")
    release = event.get("expected_release_id")
    if not isinstance(release, str) or _DIGEST.fullmatch(release) is None:
        raise ValueError("invalid shadow evaluation event")
    raw_application = event.get("expected_application_version")
    if not isinstance(raw_application, str):
        raise ValueError("invalid shadow evaluation event")
    application = application_artifact_id(raw_application)
    feeds = event.get("expected_feed_names")
    if (
        not isinstance(feeds, list)
        or not feeds
        or not all(isinstance(feed, str) and feed for feed in feeds)
        or feeds != sorted(set(feeds))
    ):
        raise ValueError("invalid shadow evaluation event")
    return release, application, tuple(feeds)


def _context(context: object) -> tuple[str, Callable[[], int]]:
    request_id = getattr(context, "aws_request_id", None)
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if not valid_invocation_id(request_id) or not callable(remaining):
        raise ValueError("invalid Lambda shadow context")
    return cast(str, request_id), cast(Callable[[], int], remaining)


def _build_runtime(configuration: _Configuration) -> ShadowRuntime:
    import boto3

    s3 = boto3.client("s3")
    outbox = InMemoryOutboxStore()
    orchestrator = WatcherOrchestrator(
        feed_state=InMemoryFeedStateStore(),
        announcement_state=InMemoryAnnouncementStateStore(),
        snapshots=InMemorySnapshotStore(max_bytes=configuration.max_response_bytes),
        outbox=outbox,
        fetcher=FeedFetcher(
            connect_timeout_seconds=configuration.connect_timeout_seconds,
            response_timeout_seconds=configuration.response_timeout_seconds,
            max_response_bytes=configuration.max_response_bytes,
        ),
        approved_hosts=configuration.approved_hosts,
        application_version=configuration.application_version,
        max_items=configuration.max_items,
        max_item_characters=configuration.max_item_characters,
        max_concurrent_fetches=configuration.max_concurrent_fetches,
        lease_seconds=configuration.lease_seconds,
    )
    return ShadowRuntime(
        release_store=S3ObjectStore(s3, configuration.config_bucket),
        pointer_key=configuration.pointer_key,
        application_version=configuration.application_version,
        orchestrator=orchestrator,
        outbox=outbox,
    )


def lambda_handler(event: Mapping[str, Any], context: object) -> dict[str, object]:
    """Evaluate the active release through ephemeral stores and bounded output."""

    expected_release, expected_application, expected_feeds = _event(event)
    configuration = _configuration_from_environment()
    invocation_id, remaining_time_ms = _context(context)
    try:
        runtime = _build_runtime(configuration)
        return runtime.run(
            invocation_id=invocation_id,
            remaining_time_ms=remaining_time_ms,
            expected_release_id=expected_release,
            expected_application_version=expected_application,
            expected_feed_names=expected_feeds,
        )
    except _ShadowRefusal as error:
        raise RuntimeError(f"shadow evaluation refused: {error.code}") from None
    except IncompatibleRelease:
        raise RuntimeError("shadow evaluation refused: release_incompatible") from None
    except ReleaseIntegrityError:
        raise RuntimeError("shadow evaluation refused: release_integrity_failure") from None
    except ObjectMissing:
        raise RuntimeError("shadow evaluation refused: release_missing") from None
    except Exception:  # noqa: BLE001 - provider and source details are not safe Lambda responses
        raise RuntimeError("shadow evaluation failed") from None
