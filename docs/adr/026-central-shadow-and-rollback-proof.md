# ADR-026: Central shadow and rollback proof

- Status: Proposed
- Date: 2026-09-03
- Owner: Lila Brooks
- Relates to: [ADR-019](019-s3-preconditions-for-release-publication-and-promotion.md), [ADR-020](020-exact-application-version-gate-for-delivery.md), [ADR-024](024-isolated-live-runtime-exercises.md)

## Context

L-42 must exercise retained configuration pointers and application objects with
their exact production-candidate identities. The persistent dev deployment
already owns that history. Recreating it in the isolated preflight deployment
would prove the mechanism against a second history.

ADR-024 isolates exercises that manufacture delivery records or send Slack
traffic. L-42 creates neither. It does temporarily move the persistent dev
active pointer and application selection, so an enabled watcher could create an
immutable candidate under the temporary release. Disabling its schedule alone
still permits an in-flight invocation or an EventBridge retry to finish.

## Decision drivers

- Exercise the retained pointer and package versions that the persistent dev
  deployment actually used.
- Prevent candidate, delivery, snapshot, validator, and Slack writes during the
  temporary configuration and application selections.
- Bind every invocation and mutation to a named role, saved plan, and exact
  read-back.
- Preserve ADR-019 promotion outcomes, including unattributed `409`
  convergence.

## Proposed decision

Deploy the L-42 shadow evaluator in the central root. It has no event source,
uses fresh in-memory state, reads only the active pointer and its pinned release
objects, and writes only its own logs. A separate operator role may invoke only
that Lambda. Asynchronous invocations have zero automatic retries; the runbook
uses synchronous invocation and records the request ID returned in the payload.

Before the first rollback mutation, apply reviewed Terraform plan bytes that
disable all four runtime triggers and set the watcher's reserved concurrency to
zero. Wait one full watcher timeout after the plan completes. Keep that state
through configuration rollback, configuration restoration, application
rollback, and application restoration. The shadow evaluator remains callable
because it has separate reserved concurrency.

Configuration rollback uses the release publisher role and the ADR-019
compare-and-swap path. Every completed apply returns the exact current pointer
identity after the compatibility probe. A converged `409` stays unattributed;
the operator records the independently read VersionId and body hash.

Application rollback changes five Lambda configurations. The watcher artifact
pair also selects the shadow evaluator package. Dispatcher and worker retain
their shared pair, and the reconciler retains its independently checked pair.
Only four trigger states exist because the shadow evaluator has no trigger.

The exercise may preview historical source replay and resolve its exact
references. It creates no replay state and sends no existing candidate again.
The two configuration pointer versions written by rollback and restoration are
retained as audit evidence.

## Failure semantics

- Any identity, role, plan hash, trigger, reserved-concurrency, release, or
  package mismatch stops the exercise.
- A shadow refusal returns one fixed reason code. Feed failures remain in the
  bounded result by feed name and error class.
- A failed or incomplete restoration leaves all four triggers disabled and the
  watcher at zero reserved concurrency until the forward state is proved or an
  incident review chooses another action.
- An unexpected durable write invalidates the exercise and requires incident
  review before another attempt.
- A quiet or failed feed sample is retained once. The operator does not repeat
  it to obtain a match.

## Consequences

The proof uses the real retained dev history and leaves two intentional pointer
versions in that history. The central root gains one Lambda, its execution and
invocation roles, and a temporary watcher execution-pause input.

The in-memory shadow run always performs unconditional feed requests and treats
announcements as first observations. Candidate identities remain comparable;
the candidate payload's `is_update` field is not evidence for this exercise.
Peak memory usage at configured limits remains unmeasured until the fixed live
sample runs.

## Options considered

### Use the isolated preflight root

This keeps every mutable object outside persistent dev. It needs a constructed
pointer history and therefore cannot exercise the exact retained dev pointer
versions named by historical candidates. Keep ADR-024 preflight for synthetic
delivery, recovery, load, and Slack exercises.

### Disable only the watcher schedule

An invocation already running can finish after the rule changes. EventBridge
may also retry an earlier failed delivery. Schedule state alone cannot prove
that candidate creation has stopped.

### Use local mocks

Local tests cover fixed refusal and concurrency outcomes. They do not prove the
deployed network path, IAM roles, Lambda package, or retained AWS object
versions.

## Verification

- Invert each event identity and confirm its fixed refusal code before feed
  work.
- Read every IAM policy attached to the shadow execution and invocation roles.
- Prove asynchronous retry count zero and synchronous invocation in the
  evidence transcript.
- Read all four trigger states and watcher reserved concurrency before the
  first rollback mutation and after final restoration.
- Record the exact active pointer VersionId and body hash after every completed
  rollback apply, including converged `409` results.
- Resolve one historical candidate's release and application references before,
  during, and after each rollback path.
- Prove the delivery tables, source-state table, queues, raw-snapshot prefix,
  and Slack destination received no exercise write.

## Migration and rollback

Acceptance requires no data migration. Terraform adds the shadow resources and
the default-false watcher pause input.

Removing this proposal removes the shadow Lambda and its two roles through a
reviewed Terraform plan. Restore the forward configuration and application
first. Keep the durable runtimes stopped until those read-backs pass.

## Revisit conditions

Revisit the central placement if the fixed live sample exceeds the 256 MB
Lambda memory limit, if the shadow path needs durable state for comparison, if
the exercise needs synthetic candidate or delivery records, or if trigger and
reserved-concurrency read-back cannot prove the watcher stopped.

## References

References verified: 2026-09-03.

- [L-42: Prove shadow evaluation and both rollback paths](https://github.com/lilabrooks/aws-public-change-feed/issues/147)
- [Acceptance and implementation sequence](../architecture/specification/06-acceptance-and-generation.md)
- [Operations runbook](../runbooks/operations.md)
