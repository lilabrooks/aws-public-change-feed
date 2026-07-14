# 3. Configuration and release model

## Documents

### Deployment configuration

`deployment.yaml` is a version 3, Git-reviewed Terraform input. It owns:

- Deployment and Region identifiers.
- Configuration bucket keys and lifecycle.
- Operational notification topic and log retention.
- Slack mode, destination metadata, credential identifiers, and rate controls.
- Environment identity, customer label, account metadata, Regions, and route.
- Feed network limits and approved hosts.
- The declared support envelope.

It contains secret identifiers, never secret values. Infrastructure-coupled keys are forbidden in runtime configuration.

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
4. Write configuration and inventory to a new release prefix with conditional create semantics.
5. Read back exact object versions and verify hashes.
6. Write the immutable release manifest.
7. Promote the active pointer only if its prior S3 version matches the publisher's observed version.
8. Run a runtime compatibility probe before announcing success.

Rollback promotes an earlier retained manifest. It never overwrites a release.

## Change review

Reviewers assess rule quality, route isolation, support-envelope impact, and source trust. New feeds require proof of ownership, stable HTTPS location, allowed host, safe fetch behavior, terms of use where applicable, and a historical sample. New Slack destinations require a credential preflight through the deployed worker.

## Examples and validators

The canonical files in `examples/` must remain mutually valid. `scripts/validate_config.py` is the local reference validator. Any new rejection path includes a mutation test in `tests/test_validate_config.py`.
