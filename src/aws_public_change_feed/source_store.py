"""AWS adapters for watcher source state and bounded raw snapshots."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, fields, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from .announcements import Provenance
from .state import (
    AnnouncementRecord,
    ConditionalStateConflict,
    FeedCheckpoint,
    FeedCompletion,
    ResponsePageMarker,
)

__all__ = [
    "DynamoDBAnnouncementStateStore",
    "DynamoDBFeedStateStore",
    "DynamoDBSourceStateStore",
    "S3SnapshotStore",
]

_INVOCATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}")
_FEED_SK = "STATE"
_ANNOUNCEMENT_SK = "STATE"
_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()
_MAX_PAGE_WRITE_RETRIES = 8


def _conditional_failed(error: Exception) -> bool:
    try:
        from botocore.exceptions import ClientError
    except ImportError:  # pragma: no cover - boto3 is a runtime dependency
        return False
    return isinstance(error, ClientError) and error.response.get("Error", {}).get("Code") == (
        "ConditionalCheckFailedException"
    )


def _wire(document: Mapping[str, object]) -> dict[str, dict[str, Any]]:
    return {name: _SERIALIZER.serialize(value) for name, value in document.items()}


def _plain(value: object) -> object:
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise ValueError("source-state numbers must be integers")
        return int(value)
    if isinstance(value, list):
        return [_plain(entry) for entry in value]
    if isinstance(value, dict):
        return {str(key): _plain(entry) for key, entry in value.items()}
    return value


def _document(item: Mapping[str, Mapping[str, Any]]) -> dict[str, object]:
    return {name: _plain(_DESERIALIZER.deserialize(value)) for name, value in item.items()}


def _without_none(document: Mapping[str, object]) -> dict[str, object]:
    return {name: value for name, value in document.items() if value is not None}


_FEED_FIELDS = {field.name for field in fields(FeedCheckpoint)}
_FEED_REQUIRED = {"feed_name", "feed_url", "consecutive_failures", "state_version"}
_ANNOUNCEMENT_FIELDS = {field.name for field in fields(AnnouncementRecord)}
_ANNOUNCEMENT_REQUIRED = {
    "announcement_id",
    "canonical_url",
    "content_fingerprint",
    "revision_id",
    "revision_ids",
    "title",
    "summary",
    "first_observed_at",
    "last_observed_at",
    "provenance",
    "emitted_candidate_ids",
    "release_ids",
    "state_version",
}
_PAGE_FIELDS = {field.name for field in fields(ResponsePageMarker)}
_PAGE_RETENTION_FIELDS = {"expires_at"}
_PAGE_REQUIRED = _PAGE_FIELDS - _PAGE_RETENTION_FIELDS


def _assert_shape(
    document: Mapping[str, object],
    *,
    allowed: set[str],
    required: set[str],
    kind: str,
) -> None:
    names = set(document)
    unknown = names - allowed
    missing = required - names
    if unknown:
        raise ValueError(f"{kind} item has unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"{kind} item is missing fields: {', '.join(sorted(missing))}")


def _feed_item(record: FeedCheckpoint) -> dict[str, object]:
    document = _without_none(asdict(record))
    return {
        "PK": f"FEED#{record.feed_name}",
        "SK": _FEED_SK,
        "item_type": "feed",
        **document,
    }


def _feed_from_item(item: Mapping[str, Mapping[str, Any]]) -> FeedCheckpoint:
    document = _document(item)
    expected_pk = f"FEED#{document.get('feed_name', '')}"
    if document.get("PK") != expected_pk or document.get("SK") != _FEED_SK:
        raise ValueError("feed item key does not agree with feed_name")
    if document.get("item_type") != "feed":
        raise ValueError("feed item has the wrong item_type")
    payload = {name: value for name, value in document.items() if name not in {"PK", "SK", "item_type"}}
    _assert_shape(payload, allowed=_FEED_FIELDS, required=_FEED_REQUIRED, kind="feed")
    return FeedCheckpoint(**payload)  # type: ignore[arg-type]


def _announcement_item(record: AnnouncementRecord) -> dict[str, object]:
    document = _without_none(asdict(record))
    return {
        "PK": f"ANNOUNCEMENT#{record.announcement_id}",
        "SK": _ANNOUNCEMENT_SK,
        "item_type": "announcement",
        **document,
    }


def _announcement_from_item(item: Mapping[str, Mapping[str, Any]]) -> AnnouncementRecord:
    document = _document(item)
    expected_pk = f"ANNOUNCEMENT#{document.get('announcement_id', '')}"
    if document.get("PK") != expected_pk or document.get("SK") != _ANNOUNCEMENT_SK:
        raise ValueError("announcement item key does not agree with announcement_id")
    if document.get("item_type") != "announcement":
        raise ValueError("announcement item has the wrong item_type")
    payload = {name: value for name, value in document.items() if name not in {"PK", "SK", "item_type"}}
    _assert_shape(
        payload,
        allowed=_ANNOUNCEMENT_FIELDS,
        required=_ANNOUNCEMENT_REQUIRED,
        kind="announcement",
    )
    provenance = payload.get("provenance")
    if not isinstance(provenance, list):
        raise ValueError("announcement provenance must be a list")
    payload["provenance"] = tuple(Provenance(**entry) for entry in provenance)
    for name in ("revision_ids", "emitted_candidate_ids", "release_ids"):
        value = payload.get(name)
        if not isinstance(value, list):
            raise ValueError(f"announcement {name} must be a list")
        payload[name] = tuple(value)
    return AnnouncementRecord(**payload)  # type: ignore[arg-type]


def _page_item(marker: ResponsePageMarker) -> dict[str, object]:
    return {
        "PK": f"RUN#{marker.run_id}",
        "SK": f"PAGESET#{marker.page_set_id}#PAGE#{marker.page:06d}",
        "item_type": "response_page",
        **_without_none(asdict(marker)),
    }


def _page_from_item(item: Mapping[str, Mapping[str, Any]]) -> ResponsePageMarker:
    document = _document(item)
    expected_pk = f"RUN#{document.get('run_id', '')}"
    page = document.get("page")
    page_set_id = document.get("page_set_id")
    expected_sk = (
        f"PAGESET#{page_set_id}#PAGE#{page:06d}"
        if isinstance(page_set_id, str) and isinstance(page, int) and not isinstance(page, bool)
        else ""
    )
    if document.get("PK") != expected_pk or document.get("SK") != expected_sk:
        raise ValueError("response-page item key does not agree with its fields")
    if document.get("item_type") != "response_page":
        raise ValueError("response-page item has the wrong item_type")
    payload = {name: value for name, value in document.items() if name not in {"PK", "SK", "item_type"}}
    _assert_shape(payload, allowed=_PAGE_FIELDS, required=_PAGE_REQUIRED, kind="response-page")
    candidate_ids = payload.get("candidate_ids")
    if not isinstance(candidate_ids, list):
        raise ValueError("response-page candidate_ids must be a list")
    payload["candidate_ids"] = tuple(candidate_ids)
    return ResponsePageMarker(**payload)  # type: ignore[arg-type]


class DynamoDBSourceStateStore:
    """Conditional feed, announcement, and response-page state in DynamoDB."""

    def __init__(self, client: Any, table_name: str) -> None:
        if not isinstance(table_name, str) or not table_name:
            raise ValueError("table_name must be a non-empty string")
        self._client = client
        self._table_name = table_name

    def _get(self, pk: str, sk: str) -> Mapping[str, Mapping[str, Any]] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=_wire({"PK": pk, "SK": sk}),
            ConsistentRead=True,
        )
        item = response.get("Item")
        return item if isinstance(item, Mapping) else None

    def load(self, key: str) -> FeedCheckpoint | AnnouncementRecord | None:
        if re.fullmatch(r"[0-9a-f]{64}", key):
            item = self._get(f"ANNOUNCEMENT#{key}", _ANNOUNCEMENT_SK)
            return None if item is None else _announcement_from_item(item)
        item = self._get(f"FEED#{key}", _FEED_SK)
        return None if item is None else _feed_from_item(item)

    def load_feed(self, feed_name: str) -> FeedCheckpoint | None:
        item = self._get(f"FEED#{feed_name}", _FEED_SK)
        return None if item is None else _feed_from_item(item)

    def load_announcement(self, announcement_id: str) -> AnnouncementRecord | None:
        item = self._get(f"ANNOUNCEMENT#{announcement_id}", _ANNOUNCEMENT_SK)
        return None if item is None else _announcement_from_item(item)

    def save(self, record: FeedCheckpoint | AnnouncementRecord) -> None:
        item = _feed_item(record) if isinstance(record, FeedCheckpoint) else _announcement_item(record)
        self._client.put_item(TableName=self._table_name, Item=_wire(item))

    def claim(
        self,
        feed_name: str,
        feed_url: str,
        *,
        owner: str,
        attempted_at: datetime,
        lease_expires_at: int,
        now: int,
    ) -> FeedCheckpoint | None:
        # Construction validates every caller-controlled scalar before DynamoDB
        # evaluates the atomic live-lease condition.
        probe = FeedCheckpoint(
            feed_name=feed_name,
            feed_url=feed_url,
            first_attempt_at=attempted_at.isoformat(),
            last_attempt_at=attempted_at.isoformat(),
            lease_owner=owner,
            lease_expires_at=lease_expires_at,
        )
        try:
            response = self._client.update_item(
                TableName=self._table_name,
                Key=_wire({"PK": f"FEED#{feed_name}", "SK": _FEED_SK}),
                UpdateExpression=(
                    "SET item_type = :type, feed_name = :name, feed_url = :url, "
                    "first_attempt_at = if_not_exists(first_attempt_at, :attempt), "
                    "last_attempt_at = :attempt, consecutive_failures = if_not_exists(consecutive_failures, :zero), "
                    "state_version = if_not_exists(state_version, :zero) + :one, "
                    "lease_owner = :owner, lease_expires_at = :lease "
                    "REMOVE pending_etag, pending_last_modified, pending_newest_publication_at, pending_run_id"
                ),
                ConditionExpression=(
                    "(attribute_not_exists(PK) "
                    "OR (attribute_not_exists(lease_owner) AND attribute_not_exists(lease_expires_at)) "
                    "OR lease_expires_at <= :now) "
                    "AND (attribute_not_exists(feed_url) OR feed_url = :url)"
                ),
                ExpressionAttributeValues=_wire(
                    {
                        ":type": "feed",
                        ":name": probe.feed_name,
                        ":url": probe.feed_url,
                        ":attempt": probe.last_attempt_at,
                        ":zero": 0,
                        ":one": 1,
                        ":owner": owner,
                        ":lease": lease_expires_at,
                        ":now": now,
                    }
                ),
                ReturnValues="ALL_NEW",
            )
        except Exception as error:
            if _conditional_failed(error):
                durable = self.load_feed(feed_name)
                if durable is not None and durable.feed_url != probe.feed_url:
                    raise ValueError("stored feed_url does not match the claimed release feed") from None
                return None
            raise
        return _feed_from_item(response["Attributes"])

    def _put_feed_conditionally(self, record: FeedCheckpoint, prior: FeedCheckpoint) -> bool:
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=_wire(_feed_item(record)),
                ConditionExpression="state_version = :version AND lease_owner = :owner",
                ExpressionAttributeValues=_wire({":version": prior.state_version, ":owner": prior.lease_owner}),
            )
        except Exception as error:
            if _conditional_failed(error):
                return False
            raise
        return True

    def mark_fetched(
        self,
        feed_name: str,
        *,
        owner: str,
        expected_state_version: int,
        etag: str | None,
        last_modified: str | None,
        newest_publication_at: datetime | None,
        run_id: str,
    ) -> FeedCheckpoint | None:
        prior = self.load_feed(feed_name)
        if prior is None or prior.lease_owner != owner or prior.state_version != expected_state_version:
            return None
        updated = replace(
            prior,
            pending_etag=etag,
            pending_last_modified=last_modified,
            pending_newest_publication_at=(
                None if newest_publication_at is None else newest_publication_at.isoformat()
            ),
            pending_run_id=run_id,
            state_version=prior.state_version + 1,
        )
        return updated if self._put_feed_conditionally(updated, prior) else None

    def complete(
        self,
        feed_name: str,
        *,
        owner: str,
        expected_state_version: int,
        succeeded_at: datetime,
    ) -> bool:
        prior = self.load_feed(feed_name)
        if prior is None or prior.lease_owner != owner or prior.state_version != expected_state_version:
            return False
        updated = prior.completed(succeeded_at)
        return self._put_feed_conditionally(updated, prior)

    def complete_many(self, completions: Iterable[FeedCompletion]) -> bool:
        requested = tuple(completions)
        if not requested:
            return True
        if len(requested) > 100:
            raise ValueError("DynamoDB transactions can complete at most 100 feeds")
        if len({entry.feed_name for entry in requested}) != len(requested):
            raise ValueError("feed completions must name each feed once")
        writes = []
        for entry in requested:
            prior = self.load_feed(entry.feed_name)
            if (
                prior is None
                or prior.lease_owner != entry.owner
                or prior.state_version != entry.expected_state_version
                or prior.pending_run_id is None
            ):
                return False
            writes.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _wire(_feed_item(prior.completed(entry.succeeded_at))),
                        "ConditionExpression": (
                            "state_version = :version AND lease_owner = :owner AND attribute_exists(pending_run_id)"
                        ),
                        "ExpressionAttributeValues": _wire(
                            {":version": prior.state_version, ":owner": prior.lease_owner}
                        ),
                    }
                }
            )
        try:
            self._client.transact_write_items(TransactItems=writes)
        except Exception as error:
            if _conditional_failed(error):
                return False
            from botocore.exceptions import ClientError

            if isinstance(error, ClientError) and error.response.get("Error", {}).get("Code") == (
                "TransactionCanceledException"
            ):
                return False
            raise
        return True

    def fail(
        self,
        feed_name: str,
        *,
        owner: str,
        expected_state_version: int,
        error_class: str,
    ) -> bool:
        prior = self.load_feed(feed_name)
        if prior is None or prior.lease_owner != owner or prior.state_version != expected_state_version:
            return False
        updated = replace(
            prior.with_failure(error_class),
            lease_owner=None,
            lease_expires_at=None,
            state_version=prior.state_version + 1,
        )
        return self._put_feed_conditionally(updated, prior)

    def put(self, record: AnnouncementRecord, *, expected_state_version: int | None) -> bool:
        item = _announcement_item(record)
        if expected_state_version is None:
            if record.state_version != 1:
                return False
            condition = "attribute_not_exists(PK)"
            values: dict[str, object] | None = None
        else:
            if record.state_version != expected_state_version + 1:
                return False
            condition = "state_version = :version"
            values = {":version": expected_state_version}
        arguments: dict[str, object] = {
            "TableName": self._table_name,
            "Item": _wire(item),
            "ConditionExpression": condition,
        }
        if values is not None:
            arguments["ExpressionAttributeValues"] = _wire(values)
        try:
            self._client.put_item(**arguments)
        except Exception as error:
            if _conditional_failed(error):
                return False
            raise
        return True

    def load_page(self, run_id: str, page_set_id: str, page: int) -> ResponsePageMarker | None:
        item = self._get(f"RUN#{run_id}", f"PAGESET#{page_set_id}#PAGE#{page:06d}")
        return None if item is None else _page_from_item(item)

    def put_page(self, marker: ResponsePageMarker) -> bool:
        """Create an exact page proof or extend its non-proof expiry metadata."""

        key = {
            "PK": f"RUN#{marker.run_id}",
            "SK": f"PAGESET#{marker.page_set_id}#PAGE#{marker.page:06d}",
        }
        for _ in range(_MAX_PAGE_WRITE_RETRIES):
            try:
                self._client.put_item(
                    TableName=self._table_name,
                    Item=_wire(_page_item(marker)),
                    ConditionExpression="attribute_not_exists(PK)",
                )
                return True
            except Exception as error:
                if not _conditional_failed(error):
                    durable = self.load_page(marker.run_id, marker.page_set_id, marker.page)
                    if durable is None:
                        raise
                    if durable != marker:
                        return False
                    if marker.expires_at is None or (
                        durable.expires_at is not None and durable.expires_at >= marker.expires_at
                    ):
                        return True

            existing = self.load_page(marker.run_id, marker.page_set_id, marker.page)
            if existing is None:
                continue
            if existing != marker:
                return False
            if marker.expires_at is None or (
                existing.expires_at is not None and existing.expires_at >= marker.expires_at
            ):
                return True

            names = {
                "#item_type": "item_type",
                "#run_id": "run_id",
                "#page_set_id": "page_set_id",
                "#feed_name": "feed_name",
                "#page": "page",
                "#candidate_ids": "candidate_ids",
                "#complete": "complete",
                "#expires_at": "expires_at",
            }
            values: dict[str, object] = {
                ":item_type": "response_page",
                ":run_id": marker.run_id,
                ":page_set_id": marker.page_set_id,
                ":feed_name": marker.feed_name,
                ":page": marker.page,
                ":candidate_ids": list(marker.candidate_ids),
                ":complete": marker.complete,
                ":expires_at": marker.expires_at,
            }
            proof_condition = (
                "#item_type = :item_type AND #run_id = :run_id AND "
                "#page_set_id = :page_set_id AND #feed_name = :feed_name AND "
                "#page = :page AND #candidate_ids = :candidate_ids AND #complete = :complete"
            )
            if existing.expires_at is None:
                expiry_condition = "attribute_not_exists(#expires_at)"
            else:
                expiry_condition = "#expires_at = :prior_expiry"
                values[":prior_expiry"] = existing.expires_at
            try:
                self._client.update_item(
                    TableName=self._table_name,
                    Key=_wire(key),
                    UpdateExpression="SET #expires_at = :expires_at",
                    ConditionExpression=f"{proof_condition} AND {expiry_condition}",
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=_wire(values),
                )
                return True
            except Exception as error:
                if _conditional_failed(error):
                    continue
                durable = self.load_page(marker.run_id, marker.page_set_id, marker.page)
                if durable is not None and durable == marker and durable.expires_at is not None:
                    if durable.expires_at >= marker.expires_at:
                        return True
                raise
        raise ConditionalStateConflict("response-page expiry lost its conditional write repeatedly")


class S3SnapshotStore:
    """Exact bounded feed-response bodies under the configured snapshot prefix."""

    def __init__(self, client: Any, bucket: str, prefix: str, *, max_bytes: int) -> None:
        if not isinstance(bucket, str) or not bucket:
            raise ValueError("bucket must be a non-empty string")
        if not isinstance(prefix, str) or not prefix or prefix.startswith("/"):
            raise ValueError("prefix must be a non-empty relative S3 prefix")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self.max_bytes = max_bytes

    def put(
        self,
        feed_name: str,
        observed_at: datetime,
        body: bytes,
        *,
        invocation_id: str = "local",
    ) -> str:
        checkpoint = FeedCheckpoint(feed_name=feed_name, feed_url="https://validation.invalid/")
        if _INVOCATION_ID.fullmatch(invocation_id) is None:
            raise ValueError("invocation_id must be a bounded opaque identifier")
        if not isinstance(body, bytes):
            raise ValueError("snapshot body must be bytes")
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("snapshot observed_at must include a UTC offset")
        if len(body) > self.max_bytes:
            raise ValueError("snapshot body exceeds max_bytes")
        digest = hashlib.sha256(body).hexdigest()
        instant = observed_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        key = f"{self._prefix}/{checkpoint.feed_name}/{instant}/{invocation_id}/{digest}.bin"
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType="application/octet-stream",
            Metadata={
                "body-sha256": digest,
                "feed-name": checkpoint.feed_name,
                "invocation-id": invocation_id,
            },
        )
        return key


class DynamoDBFeedStateStore:
    """Feed-state view of the shared DynamoDB source-state table."""

    def __init__(self, client: Any, table_name: str) -> None:
        self._source = DynamoDBSourceStateStore(client, table_name)

    def load(self, feed_name: str) -> FeedCheckpoint | None:
        return self._source.load_feed(feed_name)

    def save(self, checkpoint: FeedCheckpoint) -> None:
        self._source.save(checkpoint)

    def claim(
        self,
        feed_name: str,
        feed_url: str,
        *,
        owner: str,
        attempted_at: datetime,
        lease_expires_at: int,
        now: int,
    ) -> FeedCheckpoint | None:
        return self._source.claim(
            feed_name,
            feed_url,
            owner=owner,
            attempted_at=attempted_at,
            lease_expires_at=lease_expires_at,
            now=now,
        )

    def mark_fetched(
        self,
        feed_name: str,
        *,
        owner: str,
        expected_state_version: int,
        etag: str | None,
        last_modified: str | None,
        newest_publication_at: datetime | None,
        run_id: str,
    ) -> FeedCheckpoint | None:
        return self._source.mark_fetched(
            feed_name,
            owner=owner,
            expected_state_version=expected_state_version,
            etag=etag,
            last_modified=last_modified,
            newest_publication_at=newest_publication_at,
            run_id=run_id,
        )

    def complete(
        self,
        feed_name: str,
        *,
        owner: str,
        expected_state_version: int,
        succeeded_at: datetime,
    ) -> bool:
        return self._source.complete(
            feed_name,
            owner=owner,
            expected_state_version=expected_state_version,
            succeeded_at=succeeded_at,
        )

    def complete_many(self, completions: Iterable[FeedCompletion]) -> bool:
        return self._source.complete_many(completions)

    def fail(
        self,
        feed_name: str,
        *,
        owner: str,
        expected_state_version: int,
        error_class: str,
    ) -> bool:
        return self._source.fail(
            feed_name,
            owner=owner,
            expected_state_version=expected_state_version,
            error_class=error_class,
        )


class DynamoDBAnnouncementStateStore:
    """Announcement and response-page view of the shared source-state table."""

    def __init__(self, client: Any, table_name: str) -> None:
        self._source = DynamoDBSourceStateStore(client, table_name)

    def load(self, announcement_id: str) -> AnnouncementRecord | None:
        return self._source.load_announcement(announcement_id)

    def save(self, record: AnnouncementRecord) -> None:
        self._source.save(record)

    def put(self, record: AnnouncementRecord, *, expected_state_version: int | None) -> bool:
        return self._source.put(record, expected_state_version=expected_state_version)

    def load_page(self, run_id: str, page_set_id: str, page: int) -> ResponsePageMarker | None:
        return self._source.load_page(run_id, page_set_id, page)

    def put_page(self, marker: ResponsePageMarker) -> bool:
        return self._source.put_page(marker)
