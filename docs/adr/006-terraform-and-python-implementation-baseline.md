# ADR-006: Terraform and Python implementation baseline

- Status: Accepted
- Date: 2026-07-12

## Context

The design needs repeatable infrastructure generation and a clear runtime baseline. Native S3 state locking constrains the Terraform CLI version.

## Decision

Use Python 3.12 or newer for application and validation code. Use Terraform CLI `>= 1.10.0, < 2.0.0` until the project validates a 2.x release. Pin each AWS provider to a reviewed compatible range and commit `.terraform.lock.hcl` in every Terraform root.

Use two roots:

- `infra/bootstrap` creates the remote-state S3 bucket and optional KMS key.
- `infra/central` creates the feed watcher, state and outbox tables, FIFO queue and DLQ, Slack worker, configuration bucket objects, schedules, alarms, and IAM.

The S3 backend uses `use_lockfile = true`. Its principal receives object actions on the state object and `.tflock` object, plus `s3:ListBucket` limited to their exact prefixes. If the bucket uses a customer-managed key, grant only the KMS actions needed for encrypted state access.

Terraform validates all inputs before planning resources. TFLint and the AWS ruleset supplement native validation. Runtime packages use pinned production dependencies and reproducible build artifacts.

## Consequences

- Terraform 2.x is an intentional future qualification event instead of an assumed compatible upgrade.
- State creation stays separate from resources that consume the backend.
- Provider selections and generated plans are reproducible.
- CI must test the minimum supported Terraform and Python versions.

## References

References verified: 2026-07-13.

- [Terraform S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3)
- [Terraform dependency lock file](https://developer.hashicorp.com/terraform/language/files/dependency-lock)
- [Python 3.12 documentation](https://docs.python.org/3.12/)
