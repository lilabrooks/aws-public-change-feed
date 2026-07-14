# ADR-001: Separate deployment and runtime configuration

- Status: Accepted
- Date: 2026-07-12

## Context

Slack destinations, secret identifiers, queues, and retention settings shape AWS resources and IAM. Feed definitions, service aliases, profiles, and risk rules change more often and do not need an infrastructure plan.

## Decision

Use three versioned documents:

1. `deployment.yaml` is reviewed in Git and consumed by Terraform. It contains AWS resource settings, Slack delivery mode and destination metadata, secret resource identifiers, environment identity and route assignment, retention, and the supported scale envelope.
2. `config.yaml` contains public feeds, the service catalog, stack profiles, environment policy, risk rules, and message limits.
3. `inventory.json` is generated from deployed outputs and environment mappings. It contains no credentials.

CI publishes `config.yaml` and `inventory.json` under one immutable release prefix. Promotion writes an `active-versions.json` manifest containing the exact object keys, S3 version IDs, hashes, release ID, and schema versions. Runtime code loads that pair and rejects incompatible or mismatched documents.

Every inventory environment has exactly one policy entry: an enabled profile or an explicit disabled reason. Route IDs and environment IDs must exist in the deployment projection. Adding a route or secret requires Terraform review. Editing matching policy requires a validated release.

## Consequences

- IAM can name exact secrets and resources.
- Policy releases cannot silently change infrastructure.
- A runtime invocation uses one coherent configuration and inventory pair.
- CI needs an immutable publish and compare-and-swap promotion process.
