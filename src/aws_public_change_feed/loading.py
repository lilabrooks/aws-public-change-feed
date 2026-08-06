"""Load the active release the runtime should evaluate against.

ADR-014: "The worker loads those exact object versions and verifies hashes
before rendering." This is the read half of the release model. `releases.py`
writes the pointer; this reads it, fetches the exact versions it pins, checks
their hashes, and refuses anything the running code cannot evaluate.

The refusals are separate on purpose, because they send an operator to
different places. `IncompatibleRelease` means the bytes are intact and this
build cannot evaluate them:

- A pointer whose `schema_version` this build does not implement, or whose
  shape is missing a field this module reads.
- A configuration or inventory schema version outside the supported set. The
  runtime would misread them, which is worse than failing.
- A pinned document that hashed correctly and does not parse. Publication never
  parses what it writes, so this is reachable.

`ReleaseIntegrityError` means the stored bytes are not what the release says
they are:

- A pinned object whose hash disagrees with the pointer. The object at that
  version is not what was published.
- A pointer naming a release ID the objects it pins do not derive. The ID is a
  digest of the two hashes, so such a pointer contradicts itself, and the ID is
  what every candidate embeds.

Chapter 03's step 8 calls for a runtime compatibility probe before publication
announces success. `probe_release` is that probe: the publisher calls it with
the pointer it just promoted, and a refusal means the release is not usable by
this build even though both objects were written.

The release reference this produces is the block candidates embed. It is built
here rather than in `candidates.py` so a candidate cannot claim a release the
runtime did not load.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import yaml

from .candidates import utc_timestamp
from .identity import release_id
from .releases import POINTER_SCHEMA_VERSION, ObjectMissing, ObjectStore, StoredObject

__all__ = [
    "SUPPORTED_CONFIG_SCHEMA_VERSIONS",
    "SUPPORTED_INVENTORY_SCHEMA_VERSIONS",
    "IncompatibleRelease",
    "LoadedRelease",
    "ReleaseIntegrityError",
    "load_active_release",
    "load_release_version",
    "probe_release",
]

# The versions this build implements. A release outside these is refused rather
# than read: chapter 03 treats an incompatible version as a publication
# failure, and a runtime that reads an unknown shape "as far as it can" is how
# a silently wrong candidate gets emitted.
SUPPORTED_CONFIG_SCHEMA_VERSIONS = frozenset({4})
SUPPORTED_INVENTORY_SCHEMA_VERSIONS = frozenset({3})


class IncompatibleRelease(Exception):
    """The release is intact but this build cannot evaluate it."""


class ReleaseIntegrityError(Exception):
    """A pinned object's bytes disagree with the hash the pointer records."""


@dataclass(frozen=True, slots=True)
class LoadedRelease:
    """One active release, parsed and verified."""

    release_id: str
    config: Mapping[str, Any]
    inventory: Mapping[str, Any]
    reference: Mapping[str, Any]
    """The `release` block candidates embed.

    Built from the pointer the runtime actually loaded, so a candidate cannot
    name object versions that were never read. `validate_config.expected_release`
    checks a candidate against the same four fields.
    """

    promoted_at: str = ""
    """The promotion time recorded by the pointer this was loaded from.

    Kept so a rollback can refuse to reuse it. Empty only for a release built
    without a pointer, which no loader here produces.
    """

    def forward_document(self, promoted_at: datetime) -> dict[str, Any]:
        """Build the pointer document that restores this release.

        ADR-019: rollback writes the historical release references forward as a
        new document with a fresh `promoted_at`, and never republishes the
        historical bytes unchanged. The freshness is load-bearing rather than
        bookkeeping. Identical bytes reproduce the historical ETag, so a
        concurrent publisher still holding that ETag would find its
        precondition satisfied against a pointer that had moved away and come
        back, and its write would land on a decision nobody made.

        Refusing here rather than documenting the rule is deliberate: the
        caller supplies the timestamp, and the one value that breaks the
        guarantee is the one already sitting in the document being restored.
        """

        stamp = utc_timestamp(promoted_at)
        if stamp == self.promoted_at:
            raise ValueError(
                f"rollback must record a fresh promoted_at; {stamp} is the one the restored pointer already carries"
            )
        return {
            "schema_version": POINTER_SCHEMA_VERSION,
            "release_id": self.release_id,
            "promoted_at": stamp,
            "config": dict(self.reference["config"]),
            "inventory": dict(self.reference["inventory"]),
        }


# Every field this module reads out of a pointer reference. Checked together
# rather than at each use: reaching into an unvalidated document field by field
# turns a corrupt pointer into a `KeyError` naming one key, which says nothing
# about the release being unusable.
_REFERENCE_FIELDS = ("key", "version_id", "sha256")


def _require_usable_pointer(pointer: Mapping[str, Any]) -> None:
    """Refuse a release this build cannot evaluate, before reading any object.

    Shape and version are one check because they fail the same way for an
    operator: the release cannot be evaluated and nothing was fetched. What
    differs is only the message, so each refusal names the field it refused.
    """

    pointer_version = pointer.get("schema_version")
    if pointer_version != POINTER_SCHEMA_VERSION:
        raise IncompatibleRelease(f"active pointer schema_version {pointer_version!r} is not {POINTER_SCHEMA_VERSION}")

    identifier = pointer.get("release_id")
    if not isinstance(identifier, str) or not identifier:
        raise IncompatibleRelease(f"active pointer release_id is missing or not a string: {identifier!r}")

    supported = {
        "config": SUPPORTED_CONFIG_SCHEMA_VERSIONS,
        "inventory": SUPPORTED_INVENTORY_SCHEMA_VERSIONS,
    }
    for name, allowed in supported.items():
        reference = pointer.get(name)
        if not isinstance(reference, Mapping):
            raise IncompatibleRelease(f"active pointer is missing its {name} reference")
        for field in _REFERENCE_FIELDS:
            value = reference.get(field)
            if not isinstance(value, str) or not value:
                raise IncompatibleRelease(f"{name} reference {field} is missing or not a string: {value!r}")
        version = reference.get("schema_version")
        if version not in allowed:
            raise IncompatibleRelease(
                f"{name} schema_version {version!r} is outside the supported set {sorted(allowed)}"
            )


def _load_pinned(store: ObjectStore, reference: Mapping[str, Any], name: str) -> bytes:
    """Read one pinned object version and verify it against the pointer's hash."""

    key, version_id = reference["key"], reference["version_id"]
    try:
        stored = store.read(key, version_id)
    except ObjectMissing as missing:
        raise ReleaseIntegrityError(f"{name} version pinned by the pointer is missing: {key}") from missing
    digest = hashlib.sha256(stored.body).hexdigest()
    if digest != reference["sha256"]:
        # The pointer is the record of what should be at this version, so the
        # object is wrong rather than the pointer.
        raise ReleaseIntegrityError(
            f"{name} at the pinned version hashes to {digest}, not the {reference['sha256']} the pointer records"
        )
    return stored.body


def _parse(parser: Callable[[bytes], Any], body: bytes, name: str) -> Mapping[str, Any]:
    """Parse a pinned document, or refuse the release rather than raise raw.

    These bytes hashed correctly, so the release is intact and simply not
    something this build can evaluate, which is what `IncompatibleRelease`
    means. Publication never parses what it writes, so hash-valid unparseable
    content is reachable; letting a `yaml.ParserError` escape would put a
    fourth, untyped refusal beside the three this module documents.
    """

    try:
        document = parser(body)
    except Exception as error:
        raise IncompatibleRelease(f"{name} at the pinned version does not parse: {error}") from error
    if not isinstance(document, Mapping):
        raise IncompatibleRelease(f"{name} at the pinned version is not an object")
    return document


def _load_from_pointer(
    store: ObjectStore, current: StoredObject, label: str, application_version: str
) -> LoadedRelease:
    """Verify one pointer document and everything it pins.

    Shared by the active load and the versioned load a rollback needs. The
    runbook asks an operator to verify keys, version IDs, hashes, and schema
    versions of the release being restored, which is the same work as loading
    the active one; doing it twice in two places is how the two would come to
    disagree about what "verified" means.
    """

    try:
        pointer = json.loads(current.body)
    except ValueError as error:
        raise IncompatibleRelease(f"{label} is not JSON") from error
    if not isinstance(pointer, Mapping):
        raise IncompatibleRelease(f"{label} is not an object")

    _require_usable_pointer(pointer)

    config_body = _load_pinned(store, pointer["config"], "config")
    inventory_body = _load_pinned(store, pointer["inventory"], "inventory")

    # The release ID is derived from the two hashes, so a pointer that names an
    # ID inconsistent with the objects it pins contradicts itself. Verifying the
    # objects against the pointer is only half the check; without this the ID a
    # candidate embeds could belong to a different release entirely.
    # `validate_config.validate_manifest` makes the same comparison, which is
    # what makes trusting the stored value here a divergence from the contract
    # rather than a choice.
    derived = release_id(pointer["config"]["sha256"], pointer["inventory"]["sha256"])
    if pointer["release_id"] != derived:
        raise ReleaseIntegrityError(
            f"{label} names release {pointer['release_id']}, but the objects it pins derive {derived}"
        )

    return LoadedRelease(
        release_id=pointer["release_id"],
        promoted_at=str(pointer.get("promoted_at", "")),
        config=_parse(yaml.safe_load, config_body, "config"),
        inventory=_parse(json.loads, inventory_body, "inventory"),
        reference={
            "release_id": pointer["release_id"],
            "config": dict(pointer["config"]),
            "inventory": dict(pointer["inventory"]),
            "application_version": application_version,
        },
    )


def load_active_release(
    store: ObjectStore,
    *,
    pointer_key: str,
    application_version: str,
) -> LoadedRelease:
    """Read the active pointer and the exact object versions it pins.

    Compatibility is checked before any release object is fetched. An
    unsupported release should cost one read, and refusing early keeps the
    failure attributable to the version rather than to whatever the runtime
    tripped over while parsing bytes it should not have read.
    """

    try:
        current = store.read(pointer_key)
    except ObjectMissing as missing:
        raise ReleaseIntegrityError(f"no active release pointer at {pointer_key}") from missing
    return _load_from_pointer(store, current, f"active pointer at {pointer_key}", application_version)


def load_release_version(
    store: ObjectStore,
    *,
    pointer_key: str,
    version_id: str,
    application_version: str,
) -> LoadedRelease:
    """Read one retained pointer version and verify everything it pins.

    Runbook step 2 of a rollback: verify object keys, version IDs, hashes, and
    schema versions of the release being restored. A retained pointer version
    is only a record of what was once active, so the objects it names can have
    been deleted or the build can have moved past their schema versions. This
    is the check that turns "the pointer exists" into "this release is still
    usable", and it runs before anything is promoted.
    """

    try:
        historical = store.read(pointer_key, version_id)
    except ObjectMissing as missing:
        raise ReleaseIntegrityError(f"retained pointer version {version_id} is missing at {pointer_key}") from missing
    return _load_from_pointer(
        store,
        historical,
        f"retained pointer version {version_id}",
        application_version,
    )


def probe_release(
    store: ObjectStore,
    *,
    pointer_key: str,
    application_version: str,
    expected_release_id: str,
) -> LoadedRelease:
    """Chapter 03 step 8: prove the promoted release is usable before announcing.

    Publication writes objects and moves a pointer. Neither proves this build
    can read the result, so the probe loads the release exactly as the runtime
    will and additionally requires the pointer to name the release just
    promoted. A pointer that moved on to something else means the probe would
    be reporting on another publisher's release.
    """

    loaded = load_active_release(store, pointer_key=pointer_key, application_version=application_version)
    if loaded.release_id != expected_release_id:
        raise ReleaseIntegrityError(
            f"probe expected the pointer to name {expected_release_id}, found {loaded.release_id}"
        )
    return loaded
