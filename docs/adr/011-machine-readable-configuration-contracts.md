# ADR-011: Machine-readable configuration contracts

- Status: Accepted
- Date: 2026-07-12

## Context

Examples alone do not reject unknown fields, incomplete policies, unsafe combinations, or incompatible versions.

## Decision

Use JSON Schema draft 2020-12 for `deployment.yaml`, `config.yaml`, `inventory.json`, and `active-versions.json`. The public-feed configuration contract is version 4, deployment and inventory are version 3, and the active manifest is version 2. A configured feed declares `public_rss` or `public_atom`. Reject unknown fields at every owned object boundary.

Run schema validation first, then semantic validation for constraints that cross documents or depend on normalization. Semantic checks include release hashes, exact environment-policy coverage, profile and service references, global alias uniqueness, risk-rule overlap, route and Slack destination consistency, retry capacity, retention, and immutable release identity.

Committed examples must pass the same validator used by promotion. Each rejected mutation receives a regression test. Contract changes require a version decision and compatible runtime handling before release.

## Consequences

- Typos and stale fields fail before deployment.
- Cross-file assumptions are executable.
- Contract evolution is explicit and testable.

## References

References verified: 2026-07-13.

- [JSON Schema draft 2020-12](https://json-schema.org/draft/2020-12)
- [JSON Schema additional properties](https://json-schema.org/understanding-json-schema/reference/object#additional-properties)
