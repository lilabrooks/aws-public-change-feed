# 3. Configuration and release model

## Documents

### Deployment configuration

`deployment.yaml` is a version 3, Git-reviewed Terraform input. It owns:

- Deployment and Region identifiers.
- Configuration bucket keys and lifecycle.
- Operational notification topic, reviewed subscription aliases and protocols,
  and log retention.
- Slack mode, destination metadata, credential identifiers, and rate controls.
- Environment identity, customer label, account metadata, Regions, and route.
- Feed network limits and approved hosts.
- The declared support envelope.

It contains secret identifiers, never secret values. Infrastructure-coupled keys are forbidden in runtime configuration.

Each operational subscription descriptor is a closed object containing only a
stable alias and the reviewed protocol. The endpoint is supplied separately to
Terraform as the sensitive `operational_sns_subscription_endpoints` map, never
committed to deployment configuration. Its keys must equal the descriptor
aliases exactly. Once managed, Terraform state contains the resolved endpoint,
so the remote-state controls and access boundary protect it as sensitive
operational data.

### Runtime configuration

`config.yaml` is version 4. It owns:

- Message and contract byte limits.
- Feed, announcement, and terminal delivery retention.
- Enabled feed names, URLs, and RSS or Atom formats.
- Global service definitions and aliases.
- Service profiles.
- Exact environment policy.
- Risk rules and recommendations.

### Inventory

`inventory.json` is version 3 and generated from reviewed deployment input and Terraform outputs. It contains the runtime projection of Slack destinations and environments. Runtime code verifies that its environment and route projection exactly matches deployment input.

### Active manifest

`active-versions.json` is version 2. It names one immutable release and exact versioned S3 objects for configuration and inventory. It contains SHA-256 hashes and schema versions. Runtime never loads mutable “latest” objects by convention.

An unversioned active-manifest read that returns HTTP 403 performs one bounded
existence probe before it classifies the result. The probe calls
`ListObjectsV2` with the complete manifest key as `Prefix` and `MaxKeys=1`.
Only an exact returned key proves existence. No exact match is
`ObjectMissing`; an exact match preserves the original read denial. A failed
or malformed probe remains a refusal. Reads by exact `versionId` never use
this probe. [ADR-023](../../adr/023-scoped-active-manifest-absence-detection.md)
records the trust-boundary choice.

### Configuration bucket layout

Four kinds of object share the configuration bucket, and each has different write and retention behavior:

| Location | Written by | Versioning behavior |
| --- | --- | --- |
| `<release_prefix>/<release_id>/` | Release publisher | Write-once per release ID. Never overwritten, so these keys hold exactly one version. |
| `<active_versions_object_key>` | Release publisher | Overwritten on every promotion. Its superseded versions are the retained promotion history. |
| `<manifest directory>/raw-snapshots/` | Feed watcher | New key per fetch, deleted by lifecycle after its retention. |
| `<manifest directory>/application-artifacts/<sha256>.zip` | Package publisher; package-retirement operator deletes exact reviewed versions | One current data version per digest key, with no delete marker or version history. |

The raw-snapshot prefix is the directory of `active_versions_object_key` followed by `raw-snapshots/`. It is not a free choice for either side: the feed watcher role's `s3:PutObject` grant and the snapshot lifecycle rules are both scoped to it. `infra/central` publishes it as the `raw_snapshot_prefix` output, and the S3 `SnapshotStore` adapter reads that output rather than rebuilding the string.

The write-once shape of release keys is why release retention is not an S3 lifecycle rule. See [chapter 05](05-security-and-operations.md#retention).

`application_package_retirement` is a required deployment-owned object. Its
`retention_days` is between 400 and 3650, and its
`minimum_retained_packages` is between 10 and 100. These values govern only
content-addressed application packages. Release retirement continues to use
the separate `s3_lifecycle.retired_release_retention_days` and
`minimum_retained_releases` fields.

## Cross-document rules

Validation must prove:

- IDs are unique and references resolve.
- Every inventory environment has exactly one configuration policy.
- An enabled environment names one existing profile; a disabled environment has a nonempty reason and no profile.
- Every profile names existing services, and every service is used by at least one profile.
- Each environment route exists and resolves to one Slack destination.
- `destination_key` is unique per actual Slack destination.
- Incoming-webhook routes name one exact credential and approved host list.
- Bot routes name one channel, and destination key equals the documented workspace/channel derivation.
- Release object keys are within the configured prefix and hashes match fixture bytes.
- Release retention is at least terminal delivery retention.
- The declared global and destination delivery rates do not exceed configured pacing or the timeout-derived worker upper bound. Load evidence still establishes usable capacity.

Unknown fields fail validation.

## Service catalog and profiles

A service definition has a stable ID, display name, aliases, and recommended review action. Aliases are globally unique after normalization. Reject aliases that are generic enough to match unrelated AWS prose, including bare product categories or common verbs.

A profile is only a sorted set of service IDs. Environment policy assigns that profile to an environment. This separation prevents repeated aliases and recommendations across customer entries.

Changing service aliases, profile membership, or environment policy creates a new release. It does not change historical candidates.

A feed name bound by a retired-feed tombstone cannot silently move to another
URL. Publishing a release does not mutate source state. If that release restores
the same name and URL, the separately reviewed source-state restoration plan may
convert the tombstone into an active checkpoint. A different URL hash is refused
by the restoration tool, and the watcher also refuses to claim a tombstone.

## Risk-rule DSL

Each risk rule has a unique ID, unique risk type, priority, fields, and:

- `any`: at least one term must match.
- `all`: every term must match.
- `none`: no term may match.

At least one positive term exists across `any` and `all`. Terms are unique after normalization. A positive risk term cannot equal a service alias. Matching uses normalized field text with token or phrase boundaries; it does not use raw substring checks across markup.

The initial risk types are configuration data, not an open plugin system. Adding new rule operators requires a schema and contract decision.

## Immutable publication

1. Validate deployment, configuration, and inventory schemas.
2. Run every semantic and cross-document check.
3. Canonicalize inputs for hashing without mutating their stored representation.
4. Write configuration and inventory to a new release prefix with `If-None-Match: *`, so a create cannot overwrite an existing object.
5. Read back exact object versions and verify hashes. A read that supplies a `versionId` requires `s3:GetObjectVersion`, not `s3:GetObject`.
6. Read the active pointer and capture both its ETag and its version ID from that read. Resolve an ambiguous current-read 403 through the exact-key probe above.
7. Promote the active pointer with `If-Match` against that observed ETag. A first promotion into a new deployment uses `If-None-Match: *` instead. `active-versions.json` is the release manifest; promoting it is what records the release, and its prior versions are the retained history.
8. Run a runtime compatibility probe before announcing success.

The compatibility probe validates the pointer and both fetched document bodies against their owned schemas after exact-version hash verification. It also requires the configuration's `version` and inventory's `schema_version` to equal the corresponding version recorded in the pointer. A pointer claim cannot make incompatible bytes usable, and runtime loading rejects unknown fields just as publication validation does.

S3 has no version-ID write precondition. The ETag is the concurrency token for promotion; version IDs identify exact stored versions for reads, audit, and rollback. A failed promotion is not one condition: 412 means a competing publisher promoted first and publication stops for a fresh decision, 409 leaves the outcome indeterminate and requires re-reading the pointer, and 404 means the pointer is missing or deleted and raises an operational alarm. A 403 from the preceding current read is resolved only by the bounded probe; a visible exact key or a failed probe stops publication.

Rollback reads an earlier retained pointer version by ID and writes its release references forward through the same `If-Match` path, with a fresh `promoted_at`. It never overwrites a release, and never republishes historical bytes unchanged.

This sequence follows [ADR-019](../../adr/019-s3-preconditions-for-release-publication-and-promotion.md).

## Change review

Reviewers assess rule quality, route isolation, support-envelope impact, and source trust. New feeds require proof of ownership, stable HTTPS location, allowed host, safe fetch behavior, terms of use where applicable, and a historical sample. New Slack destinations require a credential preflight through the deployed worker.

## Examples and validators

The six committed files in `examples/` form one canonical executable contract bundle:

- `deployment.yaml`, `config.yaml`, and `inventory.json` provide a mutually consistent deployment, policy, and runtime projection.
- `active-versions.json` binds the exact configuration and inventory bytes into one immutable release.
- `alert-candidate.json` and `delivery-request.json` provide valid route-scoped output contracts tied to that release.

`scripts/validate_config.py` loads all six files directly. It validates each document against its paired schema before checking cross-document projections, references, release hashes, deterministic identities, route mapping, retention, and byte limits. `tests/test_validate_config.py` starts from the same bundle and mutates copies to prove that invalid configurations and event contracts are rejected.

The bundle is the implementation reference for future publisher, watcher, dispatcher, and worker code. Production deployments create separate reviewed values; the committed fixtures contain placeholders and test data.

Every contract change updates the affected schemas, examples, semantic checks, and mutation tests together. An edit that changes `config.yaml` or `inventory.json` bytes requires recalculating the manifest hashes and release ID. An edit to candidate identity fields requires recalculating the audience, announcement, revision, candidate, and request identities that depend on it.
