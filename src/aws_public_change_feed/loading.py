"""Load the active release the runtime should evaluate against.

ADR-014: "The worker loads those exact object versions and verifies hashes
before rendering." This is the read half of the release model. `releases.py`
writes the pointer; this reads it, fetches the exact versions it pins, checks
their hashes, and refuses anything the running code cannot evaluate.

The refusals are separate on purpose, because they send an operator to
different places. `IncompatibleRelease` means the bytes are intact and this
build cannot evaluate them:

- A pointer whose `schema_version` this build does not implement, or whose
  shape violates the owned pointer schema.
- A configuration or inventory whose internal version disagrees with the
  pointer, whose version is outside the supported set, or whose body violates
  its owned schema. The runtime would misread it, which is worse than failing.
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
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator

from .candidates import utc_timestamp
from .identity import application_artifact_id, release_id
from .parsing import load_unique_json, load_unique_yaml
from .releases import POINTER_SCHEMA_VERSION, ObjectMissing, ObjectStore, StoredObject
from .schema_formats import contract_format_checker

__all__ = [
    "SUPPORTED_CONFIG_SCHEMA_VERSIONS",
    "SUPPORTED_INVENTORY_SCHEMA_VERSIONS",
    "IncompatibleRelease",
    "LoadedRelease",
    "ReleaseIntegrityError",
    "load_active_release",
    "load_release_reference",
    "load_release_version",
    "probe_release",
]

# The versions this build implements. A release outside these is refused rather
# than read: chapter 03 treats an incompatible version as a publication
# failure, and a runtime that reads an unknown shape "as far as it can" is how
# a silently wrong candidate gets emitted.
SUPPORTED_CONFIG_SCHEMA_VERSIONS = frozenset({4})
SUPPORTED_INVENTORY_SCHEMA_VERSIONS = frozenset({3})

_SCHEMA_RESOURCES = {
    "pointer": "active-versions.schema.json",
    "config": "config.schema.json",
    "inventory": "inventory.schema.json",
}


def _schema_validator(name: str) -> Draft202012Validator:
    """Load one packaged copy of an owned schema and compile it once.

    The package copies are checked against the authoritative top-level schema
    files by the test suite. Keeping them as resources lets an installed wheel
    enforce the same boundary without depending on a source checkout.
    """

    resource = files("aws_public_change_feed.schemas").joinpath(_SCHEMA_RESOURCES[name])
    schema = json.loads(resource.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=contract_format_checker())


_SCHEMA_VALIDATORS = {name: _schema_validator(name) for name in _SCHEMA_RESOURCES}


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

    def forward_document(self, promoted_at: datetime) -> dict[str, Any]:
        """Build the pointer document that restores this release.

        ADR-019: rollback writes the historical release references forward as a
        new document with a fresh `promoted_at`, and never republishes the
        historical bytes unchanged. The freshness is enforced in
        `promote_pointer`, which holds the pointer being replaced and so can
        require the new time to follow it. An audit found the rule cannot be
        enforced here: comparing against the version being restored misses the
        case where restoring, promoting away, and restoring again reproduces
        the first rollback's bytes rather than the original's.
        """

        return {
            "schema_version": POINTER_SCHEMA_VERSION,
            "release_id": self.release_id,
            "promoted_at": utc_timestamp(promoted_at),
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

    # Required by the schema, and load-bearing rather than descriptive: the
    # promotion guard compares against it, so an absent or non-string value
    # would leave that guard unable to fire while everything still looked fine.
    stamp = pointer.get("promoted_at")
    if not isinstance(stamp, str) or not stamp:
        raise IncompatibleRelease(f"active pointer promoted_at is missing or not a string: {stamp!r}")

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

    _require_schema("active pointer", pointer, "pointer")


def _require_schema(label: str, document: Mapping[str, Any], schema_name: str) -> None:
    """Refuse the first owned-schema violation with a useful document path."""

    violation = next(_SCHEMA_VALIDATORS[schema_name].iter_errors(document), None)
    if violation is None:
        return
    path = ".".join(str(part) for part in violation.absolute_path)
    location = f" at {path}" if path else ""
    raise IncompatibleRelease(f"{label} violates its owned schema{location}: {violation.message}")


def _require_document_version(
    name: str,
    document: Mapping[str, Any],
    reference: Mapping[str, Any],
) -> None:
    """Bind the fetched document's version to the version the pointer claims."""

    field = "version" if name == "config" else "schema_version"
    actual = document.get(field)
    claimed = reference["schema_version"]
    if actual != claimed:
        raise IncompatibleRelease(
            f"{name} document {field} {actual!r} does not match the pointer's schema_version {claimed!r}"
        )
    _require_schema(name, document, name)


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

    application_version = application_artifact_id(application_version)

    try:
        pointer = load_unique_json(current.body)
    except ValueError as error:
        raise IncompatibleRelease(f"{label} is not JSON: {error}") from error
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

    config = _parse(load_unique_yaml, config_body, "config")
    inventory = _parse(load_unique_json, inventory_body, "inventory")
    _require_document_version("config", config, pointer["config"])
    _require_document_version("inventory", inventory, pointer["inventory"])

    return LoadedRelease(
        release_id=pointer["release_id"],
        config=config,
        inventory=inventory,
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
    return _load_from_pointer(
        store,
        current,
        f"active pointer at {pointer_key}",
        application_version,
    )


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


def load_release_reference(
    store: ObjectStore,
    reference: Mapping[str, Any],
    *,
    application_version: str,
) -> LoadedRelease:
    """Load and verify the exact release a candidate embeds.

    ADR-014: the worker "loads those exact object versions and verifies hashes
    before rendering". A candidate carries the full release reference, so unlike
    `load_active_release` and `load_release_version` there is no pointer to read:
    this loads the config and inventory at the pinned versions, verifies each
    hash, derives the release ID from those hashes and refuses a reference whose
    ID contradicts its own objects, binds schema versions, and validates the
    fetched bodies against their owned schemas. The refusals are the same typed
    ones the pointer path raises, so an operator sees the same causes for the
    same bytes whether the block came from the active pointer or a candidate.
    """

    application_version = application_artifact_id(application_version)

    config_ref = reference["config"]
    inventory_ref = reference["inventory"]
    supported = {
        "config": SUPPORTED_CONFIG_SCHEMA_VERSIONS,
        "inventory": SUPPORTED_INVENTORY_SCHEMA_VERSIONS,
    }
    for name, allowed in supported.items():
        version = reference[name]["schema_version"]
        if version not in allowed:
            raise IncompatibleRelease(
                f"{name} schema_version {version!r} is outside the supported set {sorted(allowed)}"
            )

    derived = release_id(config_ref["sha256"], inventory_ref["sha256"])
    if reference["release_id"] != derived:
        raise ReleaseIntegrityError(
            f"candidate release names {reference['release_id']}, but its objects derive {derived}"
        )

    config_body = _load_pinned(store, config_ref, "config")
    inventory_body = _load_pinned(store, inventory_ref, "inventory")
    config = _parse(load_unique_yaml, config_body, "config")
    inventory = _parse(load_unique_json, inventory_body, "inventory")
    _require_document_version("config", config, config_ref)
    _require_document_version("inventory", inventory, inventory_ref)

    return LoadedRelease(
        release_id=reference["release_id"],
        config=config,
        inventory=inventory,
        reference={
            "release_id": reference["release_id"],
            "config": dict(config_ref),
            "inventory": dict(inventory_ref),
            "application_version": application_version,
        },
    )
