"""Immutable release publication and active-pointer promotion.

Chapter 03 "Immutable publication" numbers the sequence and ADR-019 fixes the
HTTP preconditions it uses. This module implements steps 3 through 7: hash the
canonical inputs, create the release objects with `If-None-Match: *`, read each
back by exact version and verify, then promote the pointer with `If-Match`
against an ETag observed in the same read that produced the decision.

Steps 1 and 2 belong to `scripts/validate_config.py`, which already validates
schemas and cross-document rules. Step 8, the runtime compatibility probe, is
`loading.probe_release`: this module stops at a promoted pointer, and the
publisher proves the result is readable by loading it back before announcing
success.

The object store is a port, and the three exceptions it raises are named after
HTTP outcomes rather than after meanings. That is deliberate: ADR-019 gives 412
different consequences on the create path and the promote path, so the status
is the fact the adapter reports and the interpretation belongs here. Collapsing
them into one "condition failed" is what this contract exists to prevent.

The port is also what makes the indeterminate 409 testable. It cannot be
provoked against a real bucket or against the mock, so it is raised at this
seam instead; ADR-019's milestone-2 testing section records the measurements
behind that decision.

Nothing here retries a failed promotion. Promotion chooses which release is
active, so a blind retry would replace a competing publisher's decision with a
stale one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

# `utc_timestamp` formats a contract timestamp and rejects naive datetimes.
# Imported rather than repeated: two spellings of the same rule is how a
# candidate and a pointer end up disagreeing about what time it is.
from .candidates import utc_timestamp
from .identity import release_id

__all__ = [
    "CREATE_CONFLICT_ATTEMPTS",
    "POINTER_SCHEMA_VERSION",
    "ObjectMissing",
    "ObjectStore",
    "PointerVanished",
    "PreconditionFailed",
    "Promotion",
    "PromotionSuperseded",
    "PublishedObject",
    "ReleaseArtifacts",
    "S3ObjectStore",
    "StoredObject",
    "WriteConflict",
    "promote_pointer",
    "publish_objects",
    "release_keys",
]

POINTER_SCHEMA_VERSION = 2

# ADR-019 bounds the create retry rather than leaving it open: a delete racing
# a create is transient, but an unbounded retry against a persistent 409 would
# hold publication open indefinitely.
CREATE_CONFLICT_ATTEMPTS = 3


class PreconditionFailed(Exception):
    """412. A conditional write's precondition did not hold.

    On a create this means an object already exists at the key. On a promotion
    it means the pointer moved between the read and the write. Same status,
    different consequence, which is why the caller decides.
    """


class ObjectMissing(Exception):
    """404. No current version exists, or the current version is a delete marker."""


class WriteConflict(Exception):
    """409. A concurrent request left the outcome indeterminate."""


class PromotionSuperseded(Exception):
    """Another publisher promoted between the read and the write.

    Carries both release IDs because ADR-019 requires the failure to report
    them: an operator deciding whether to republish needs to know which release
    won, not only that a precondition failed.
    """

    def __init__(self, promoting: str, observed: str | None) -> None:
        super().__init__(
            f"pointer moved before promotion: promoting {promoting}, pointer now names {observed or '<unreadable>'}"
        )
        self.promoting = promoting
        self.observed = observed


class PointerVanished(Exception):
    """The pointer was expected to exist and does not.

    ADR-019 makes this an operational alarm rather than a retry: falling back
    to a create would turn a deleted pointer into a silent re-creation.
    """


@dataclass(frozen=True, slots=True)
class StoredObject:
    """One object version as read from the store."""

    body: bytes
    etag: str
    version_id: str


@dataclass(frozen=True, slots=True)
class PublishedObject:
    """A release object that exists at a known version with a verified hash."""

    key: str
    version_id: str
    sha256: str
    schema_version: int
    adopted: bool = False
    """True when the object already existed carrying identical bytes.

    Release keys embed the content-derived release ID, so republishing the same
    inputs is expected to find its own objects. That is not a failure. The
    distinction is kept because an operator reading a publication record should
    be able to tell a fresh write from an adoption.
    """


@dataclass(frozen=True, slots=True)
class ReleaseArtifacts:
    """Both release objects, verified, before any pointer is written."""

    release_id: str
    config: PublishedObject
    inventory: PublishedObject

    def pointer_document(self, promoted_at: datetime) -> dict[str, Any]:
        """Build the `active-versions.json` document for these artifacts."""

        return {
            "schema_version": POINTER_SCHEMA_VERSION,
            "release_id": self.release_id,
            "promoted_at": utc_timestamp(promoted_at),
            "config": _reference(self.config),
            "inventory": _reference(self.inventory),
        }


@dataclass(frozen=True, slots=True)
class Promotion:
    """The outcome of one pointer write."""

    release_id: str
    new_version_id: str | None
    prior_version_id: str | None
    prior_release_id: str | None
    converged: bool = False
    """True when a 409 left attribution unknown and a re-read found this release.

    ADR-019 is explicit that this records convergence, not success: a competing
    publisher promoting the same release produces an identical pointer, and S3
    does not attribute the winning write.
    """


class ObjectStore(Protocol):
    """The narrow S3 surface publication needs, with outcomes preserved."""

    def create(self, key: str, body: bytes) -> str:
        """Write with `If-None-Match: *`; return the new version ID.

        Raises `PreconditionFailed` on 412 and `WriteConflict` on 409.
        """
        ...

    def read(self, key: str, version_id: str | None = None) -> StoredObject:
        """Read the current object, or an exact version when one is given.

        Raises `ObjectMissing` when there is no current version.
        """
        ...

    def replace(self, key: str, body: bytes, *, if_match: str) -> str:
        """Write with `If-Match`; return the new version ID.

        Raises `PreconditionFailed` on 412, `WriteConflict` on 409, and
        `ObjectMissing` on 404.
        """
        ...


class S3ObjectStore:
    """`ObjectStore` backed by S3, translating error codes into outcomes.

    The translation is the whole adapter. `tests/test_s3_preconditions.py`
    pins the mock's behavior for every status this maps, so a dependency bump
    that changed one would fail there by name rather than here as a confusing
    publication error.
    """

    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @staticmethod
    def _status_of(error: Any) -> int:
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return int(status) if status is not None else 0

    def _translate(self, error: Exception) -> Exception:
        # Typed as `Exception` rather than the SDK's error class so an
        # unrecognized status is returned unchanged with its type intact.
        # `_status_of` takes `Any` because the attribute it reads exists only
        # on botocore's class, which ships no type information.
        status = self._status_of(error)
        if status == 412:
            return PreconditionFailed(str(error))
        if status == 409:
            return WriteConflict(str(error))
        if status == 404:
            return ObjectMissing(str(error))
        return error

    def create(self, key: str, body: bytes) -> str:
        from botocore.exceptions import ClientError

        try:
            response = self._client.put_object(Bucket=self._bucket, Key=key, Body=body, IfNoneMatch="*")
        except ClientError as error:
            raise self._translate(error) from error
        return str(response["VersionId"])

    def read(self, key: str, version_id: str | None = None) -> StoredObject:
        from botocore.exceptions import ClientError

        arguments: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if version_id is not None:
            arguments["VersionId"] = version_id
        try:
            response = self._client.get_object(**arguments)
        except ClientError as error:
            if version_id is None and self._status_of(error) == 403:
                self._raise_current_read_outcome(key, error)
            raise self._translate(error) from error
        return StoredObject(
            body=response["Body"].read(),
            etag=str(response["ETag"]),
            version_id=str(response["VersionId"]),
        )

    def _raise_current_read_outcome(self, key: str, read_error: Exception) -> None:
        """Resolve an ambiguous current-object 403 with one exact-key probe."""

        from botocore.exceptions import ClientError

        try:
            response = self._client.list_objects_v2(Bucket=self._bucket, Prefix=key, MaxKeys=1)
        except ClientError as list_error:
            raise list_error from read_error

        if not isinstance(response, dict):
            raise read_error
        contents = response.get("Contents", [])
        if not isinstance(contents, list) or len(contents) > 1:
            raise read_error

        exact_key_found = False
        for item in contents:
            if not isinstance(item, dict) or not isinstance(item.get("Key"), str):
                raise read_error
            if item["Key"] == key:
                exact_key_found = True

        if exact_key_found:
            raise read_error
        raise ObjectMissing(str(read_error)) from read_error

    def replace(self, key: str, body: bytes, *, if_match: str) -> str:
        from botocore.exceptions import ClientError

        try:
            response = self._client.put_object(Bucket=self._bucket, Key=key, Body=body, IfMatch=if_match)
        except ClientError as error:
            raise self._translate(error) from error
        return str(response["VersionId"])


def _reference(published: PublishedObject) -> dict[str, Any]:
    return {
        "key": published.key,
        "version_id": published.version_id,
        "sha256": published.sha256,
        "schema_version": published.schema_version,
    }


def _named_release(body: bytes) -> str | None:
    """Return the release ID a pointer document names, or None if unreadable."""

    try:
        document = json.loads(body)
    except ValueError:
        return None
    identifier = document.get("release_id") if isinstance(document, dict) else None
    return str(identifier) if isinstance(identifier, str) else None


def _current_release(store: ObjectStore, pointer_key: str) -> str | None:
    """Re-read the pointer and report which release it names."""

    try:
        return _named_release(store.read(pointer_key).body)
    except ObjectMissing:
        return None


def release_keys(prefix: str, identifier: str, config_filename: str, inventory_filename: str) -> tuple[str, str]:
    """Return the config and inventory keys for one release.

    The layout matches `validate_manifest`, which builds the same keys from
    `deployment.yaml` and rejects a pointer that disagrees. Deriving them in one
    place is what keeps the publisher and the validator from drifting apart.
    """

    root = f"{prefix.rstrip('/')}/{identifier}"
    return f"{root}/{config_filename}", f"{root}/{inventory_filename}"


def _create_with_bounded_retry(store: ObjectStore, key: str, body: bytes) -> str:
    """Create one object, retrying only the indeterminate 409."""

    for remaining in range(CREATE_CONFLICT_ATTEMPTS - 1, -1, -1):
        try:
            return store.create(key, body)
        except WriteConflict:
            if remaining == 0:
                raise
    raise AssertionError("unreachable: the loop returns or raises")


def _publish_one(store: ObjectStore, key: str, body: bytes, schema_version: int) -> PublishedObject:
    """Create or adopt one release object, then verify it by exact version."""

    digest = hashlib.sha256(body).hexdigest()
    adopted = False
    try:
        version_id = _create_with_bounded_retry(store, key, body)
    except PreconditionFailed:
        # Release keys embed the content-derived release ID, so an existing
        # object is expected to hold identical bytes. Anything else is an
        # out-of-band write rather than a concurrency event, and publication
        # fails instead of adopting content nobody published.
        existing = store.read(key)
        if hashlib.sha256(existing.body).hexdigest() != digest:
            raise ValueError(f"release object already exists with different content: {key}") from None
        version_id, adopted = existing.version_id, True

    # Step 5 reads back the exact version rather than the current object. The
    # pointer pins this version ID, so this version's hash is the one that has
    # to match; a concurrent create at the same key would not change it.
    stored = store.read(key, version_id)
    if hashlib.sha256(stored.body).hexdigest() != digest:
        raise ValueError(f"release object read back with a different hash: {key}")
    return PublishedObject(
        key=key,
        version_id=version_id,
        sha256=digest,
        schema_version=schema_version,
        adopted=adopted,
    )


def publish_objects(
    store: ObjectStore,
    *,
    config_body: bytes,
    inventory_body: bytes,
    config_schema_version: int,
    inventory_schema_version: int,
    release_prefix: str,
    config_filename: str,
    inventory_filename: str,
) -> ReleaseArtifacts:
    """Chapter 03 steps 3 to 5: hash, create immutably, verify by version.

    Takes bytes rather than paths. The release ID is derived from the exact
    bytes published, so a caller that re-serialized a document would compute an
    identity for content the store never held.
    """

    identifier = release_id(
        hashlib.sha256(config_body).hexdigest(),
        hashlib.sha256(inventory_body).hexdigest(),
    )
    config_key, inventory_key = release_keys(release_prefix, identifier, config_filename, inventory_filename)
    return ReleaseArtifacts(
        release_id=identifier,
        config=_publish_one(store, config_key, config_body, config_schema_version),
        inventory=_publish_one(store, inventory_key, inventory_body, inventory_schema_version),
    )


def _converged_or_superseded(
    store: ObjectStore,
    pointer_key: str,
    promoting: str,
    observed: StoredObject | None,
) -> Promotion:
    """Resolve an indeterminate 409 by re-reading the pointer.

    ADR-019: if the pointer names this release, promotion has converged and the
    publisher records the desired release as active. It does not record that
    this request succeeded, because a competing publisher promoting the same
    release produces an identical pointer and S3 does not attribute the winning
    write. Anything else is not promoted.
    """

    current = _current_release(store, pointer_key)
    if current != promoting:
        raise PromotionSuperseded(promoting, current)
    return Promotion(
        release_id=promoting,
        new_version_id=None,
        prior_version_id=None if observed is None else observed.version_id,
        prior_release_id=None if observed is None else _named_release(observed.body),
        converged=True,
    )


def _promoted_at(body: bytes) -> datetime | None:
    """Return the promotion time a pointer document records, or None."""

    try:
        document = json.loads(body)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    raw = document.get("promoted_at")
    if not isinstance(raw, str) or not raw:
        return None
    text = f"{raw[:-1]}+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _require_forward_promotion(document: bytes, observed: StoredObject | None) -> None:
    """Refuse a promotion that could reproduce a retained version's ETag.

    ADR-019 requires rollback never to republish historical bytes unchanged,
    because identical bytes reproduce the historical ETag and a concurrent
    publisher still holding it would find its precondition satisfied against a
    pointer that had moved away and come back. Comparing only against the
    version being restored is too narrow: restoring a release, promoting away,
    and restoring it again with the same timestamp reproduces the *first
    rollback's* bytes, which that comparison never sees.

    A promotion time strictly later than the pointer being replaced makes the
    whole family unreachable, including re-promoting the same release, without
    reading every retained version. A pointer whose own time cannot be parsed
    is replaceable, or a malformed pointer could not be corrected.
    """

    promoting = _promoted_at(document)
    if promoting is None:
        raise ValueError("pointer document does not record a parseable promoted_at")
    if observed is None:
        return
    replaced = _promoted_at(observed.body)
    if replaced is not None and promoting <= replaced:
        raise ValueError(
            f"promotion must record a time after the pointer it replaces: "
            f"{promoting.isoformat()} does not follow {replaced.isoformat()}"
        )


def promote_pointer(
    store: ObjectStore,
    *,
    pointer_key: str,
    document: bytes,
    observed: StoredObject | None,
) -> Promotion:
    """Chapter 03 steps 6 and 7: compare-and-swap the active pointer.

    `observed` is the pointer read that produced the decision to promote, or
    None when that read found no pointer. ADR-019 requires the ETag to come
    from that same read: a precondition read taken separately from the decision
    is not a compare-and-swap, and taking the whole read rather than a bare
    ETag makes supplying a detached one awkward on purpose.

    The release being promoted is read out of `document` rather than accepted
    alongside it. Passing both invites them to disagree, and the disagreement
    is not visible: the 409 branch compares the re-read pointer against the
    promoted release, so a mismatched pair reports a converged promotion as a
    lost one.
    """

    promoting = _named_release(document)
    if promoting is None:
        raise ValueError("pointer document does not name a release_id")
    _require_forward_promotion(document, observed)

    if observed is None:
        # First promotion into a new deployment. A 412 here means the pointer
        # exists after all, so the publisher restarts from a fresh read rather
        # than falling back, which would re-create a deleted pointer.
        try:
            version_id = store.create(pointer_key, document)
        except PreconditionFailed:
            raise PromotionSuperseded(promoting, _current_release(store, pointer_key)) from None
        except WriteConflict:
            # A create can be left indeterminate exactly as a replace can, and
            # it resolves the same way. Leaving this uncaught would put an
            # undefined outcome on the one path a new deployment always takes.
            return _converged_or_superseded(store, pointer_key, promoting, observed)
        return Promotion(
            release_id=promoting,
            new_version_id=version_id,
            prior_version_id=None,
            prior_release_id=None,
        )

    try:
        version_id = store.replace(pointer_key, document, if_match=observed.etag)
    except PreconditionFailed:
        raise PromotionSuperseded(promoting, _current_release(store, pointer_key)) from None
    except ObjectMissing:
        raise PointerVanished(
            f"active pointer {pointer_key} was expected to exist and does not; "
            "promotion stops and raises an operational alarm"
        ) from None
    except WriteConflict:
        return _converged_or_superseded(store, pointer_key, promoting, observed)

    return Promotion(
        release_id=promoting,
        new_version_id=version_id,
        prior_version_id=observed.version_id,
        prior_release_id=_named_release(observed.body),
    )
