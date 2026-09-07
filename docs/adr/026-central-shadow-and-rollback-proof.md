# ADR-026: Central shadow and rollback proof

- Status: Accepted
- Date: 2026-09-05
- Owner: Lila Brooks
- Revision accepted: 2026-09-06
- Application-only revision accepted: 2026-09-06
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

## Decision

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

ADR-029 now governs package provenance and target handler evidence for new
packages. L-53 still decides how that evidence changes rollback eligibility and
whether the exact checksum-less `c88b49c8...` package receives a bounded legacy
exception. This decision does not grant that exception.

## Accepted 2026-09-06 revision: L-53 rollback eligibility and quiescence

The repository owner accepted this revision on 2026-09-06. Live rollout and
rollback still require their separate exact-action authorizations.

Keep the five-function package boundary for the current handlers. Grant a
legacy checksum exception only to package digest
`c88b49c8f070f1cb808ac005cbe28b484c14be7b29f50a34e21ef3a7ca85ccbd`
at S3 VersionId `QXNwt_NBIqp0pNKVFalwbZ72587h.GCc`. Before downtime, read
those exact object bytes, recompute the digest, reject unsafe archive members,
match the complete owned source tree to revision
`bb6add6e881249d75b4b7243e4def9528be23a47`, validate all five handler
declarations, and pass the existing Linux Python 3.12 x86_64 import gate
without changing the ZIP. The L-42 record supplies prior-deployment evidence;
current Lambda read-back must confirm the same package pair. Any failure stops
before service pause.

Every other worker/shared-runtime or reconciler selection must provide the S3
SHA-256 checksum and pass exact checksum read-back. There is no nullable general
exception, handler-digest allowlist, or legacy publisher. The retirement role
must carry an explicit deny for the fixed legacy digest key. The old VersionId
is non-restorable after deletion; separately authorized recovery of identical
bytes would create a different VersionId and would require a new decision.

After acquisition and scheduling stop and the worker drains, disable its event
source and set reserved concurrency to zero for watcher, dispatcher, worker,
and reconciler. Read back all four disabled triggers and all four zero
concurrency values, then wait the longest active-invocation timeout. Keep the
shadow evaluator callable at concurrency one. Preserve this state through
rollback and forward restoration. Restore the original concurrency values and
trigger states only after the forward package and references pass read-back.

Rollback eligibility is distinct from historical replay availability. The
archived L-42 snapshot records 15 posted deliveries and no actionable work, so
its three older package references do not prove stranded delivery. It also
does not qualify those packages for current Terraform deployment. L-53 does
not grant blanket replay support to old packages. A current actionable record
whose exact package is unavailable remains unchanged under ADR-020's
`artifact_unavailable` path and needs an explicit restoration or manual-closure
decision.

The alternatives were a four-durable-function rollback with shadow still on
forward code, an independent shadow artifact pair, or making the old package
ineligible. Forward shadow code would not evaluate the package selected for
rollback. An independent pair adds another package contract and still needs a
compatible historical shadow handler. Refusing the old package avoids a legacy
exception but leaves no genuine predecessor for the accepted five-function
exercise. A general legacy republisher was also rejected because it would turn
one reviewed exception into an open compatibility surface.

If exact-byte or target-import qualification fails, withdraw the exception and
stop L-54 before downtime. Reverting the implementation restores the existing
watcher-only pause and optional-checksum behavior, so that revert is safe only
while triggers remain disabled and no plan has selected a package under this
revision.

## Accepted 2026-09-06 revision: application-only L-54 follow-up

The repository owner accepted this revision on 2026-09-06 after correcting its
evidence-invalidation rule, stopped-interval boundary, and fallback language.
Live plans and invocations still require separate exact-action authorization.

L-54 may close only the application-proof gap left by L-42, without writing
new configuration pointer versions, when every one of these preconditions is
proved before the maintenance pause:

- The checked-in L-42 evidence record still has SHA-256
  `80ef235d43891895fdb92f90975c9c285001363dbbefe042fb1ebfb0cd3b7fcf`,
  and its restricted evidence manifest is available and recomputes to
  `ffdf5cfade61eee19efeabccbc8c62035359607535b7a0a03ae2910a66994854`.
- A recorded comparison from the L-42 implementation and evidence baseline to
  the current exercise identifies intervening changes and explains whether
  each can affect the configuration claims being reused: exact retained-version
  reads, release identity and integrity, pointer compare-and-swap promotion,
  compatibility probing, and exact pointer read-back. An unresolved material
  difference blocks reuse. A recorded unrelated change does not by itself
  require another live configuration exercise.
- The active pointer and one retained pointer resolve read-only to their exact
  configuration and inventory objects, both releases pass the application
  compatibility probe for the package being evaluated, and a retained-source
  replay preview resolves its historical application and release references
  without apply.
- The current active release remains selected throughout the stopped proof
  interval. No release publication, pointer promotion, replay apply, delivery
  manufacture, Slack post, or unexpected application write may occur during
  that interval.

After those checks, L-54 uses the accepted drain, four-executor pause, and
five-function package boundary. One unchanged saved Terraform plan selects the
qualified `c88b49c8...` predecessor across all five functions. Read-back binds
the exact package and references, and one bounded shadow invocation evaluates
that package against the still-active release. A second saved plan restores
the exact `1ae996ca...` successor. Read-back and one bounded shadow invocation
must pass again before the recorded concurrency and triggers resume.

Capture the durable-state baseline after drain, confirmed full execution
pause, and the required timeout. Compare it after forward-package verification
and before resumption. The prohibition on Slack posts and unexpected
application writes applies to this stopped proof interval. Verify restored
controls, alarms, and normal operation separately after resumption. Those
post-resumption checks may legitimately create state or send a Slack message;
they do not alter the stopped-interval no-write result. A quiet public-feed
result remains valid evidence.

The application-only result is `passed` only when L-42's configuration proof
passes the reuse preconditions and the backward and forward application proof
passes in full. A missing evidence bundle, unresolved material difference,
failed reference resolution, pointer change, unexpected stopped-interval
write, failed restoration, or incomplete read-back refuses reuse. The result
then remains `failed` or `incomplete`.

L-42's combined procedure and evidence remain historical fact. The combined
procedure is not an automatic fallback. Changes or unavailable evidence
require reassessing the affected claim. Perform only the targeted checks or
separately accepted live transition needed to establish that claim.
Configuration mutation is never an automatic fallback.

## References

References verified: 2026-09-03.

- [L-42: Prove shadow evaluation and both rollback paths](https://github.com/lilabrooks/aws-public-change-feed/issues/147)
- [Acceptance and implementation sequence](../architecture/specification/06-acceptance-and-generation.md)
- [Operations runbook](../runbooks/operations.md)
