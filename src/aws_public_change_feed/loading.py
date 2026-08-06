"""Load the active release the runtime should evaluate against.

ADR-014: "The worker loads those exact object versions and verifies hashes
before rendering." This is the read half of the release model. `releases.py`
writes the pointer; this reads it, fetches the exact versions it pins, checks
their hashes, and refuses anything the running code cannot evaluate.

Three refusals matter and are separate on purpose:

- A pointer whose `schema_version` this build does not implement. The document
  shape itself is unreadable, so nothing further can be trusted.
- A configuration or inventory schema version outside the supported set. The
  bytes are intact and the runtime would misread them, which is worse than
  failing.
- A hash that disagrees with the pointer. The object at that version is not
  what was published, and the pointer is the only record of what should be
  there.

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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from .releases import POINTER_SCHEMA_VERSION, ObjectMissing, ObjectStore

__all__ = [
    "SUPPORTED_CONFIG_SCHEMA_VERSIONS",
    "SUPPORTED_INVENTORY_SCHEMA_VERSIONS",
    "IncompatibleRelease",
    "LoadedRelease",
    "ReleaseIntegrityError",
    "load_active_release",
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


def _require_versions(pointer: Mapping[str, Any]) -> None:
    """Refuse a release this build cannot evaluate, before reading any object."""

    pointer_version = pointer.get("schema_version")
    if pointer_version != POINTER_SCHEMA_VERSION:
        raise IncompatibleRelease(f"active pointer schema_version {pointer_version!r} is not {POINTER_SCHEMA_VERSION}")
    supported = {
        "config": SUPPORTED_CONFIG_SCHEMA_VERSIONS,
        "inventory": SUPPORTED_INVENTORY_SCHEMA_VERSIONS,
    }
    for name, allowed in supported.items():
        reference = pointer.get(name)
        if not isinstance(reference, Mapping):
            raise IncompatibleRelease(f"active pointer is missing its {name} reference")
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

    try:
        pointer = json.loads(current.body)
    except ValueError as error:
        raise IncompatibleRelease(f"active pointer at {pointer_key} is not JSON") from error
    if not isinstance(pointer, Mapping):
        raise IncompatibleRelease(f"active pointer at {pointer_key} is not an object")

    _require_versions(pointer)

    config_body = _load_pinned(store, pointer["config"], "config")
    inventory_body = _load_pinned(store, pointer["inventory"], "inventory")

    return LoadedRelease(
        release_id=pointer["release_id"],
        config=yaml.safe_load(config_body),
        inventory=json.loads(inventory_body),
        reference={
            "release_id": pointer["release_id"],
            "config": dict(pointer["config"]),
            "inventory": dict(pointer["inventory"]),
            "application_version": application_version,
        },
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
