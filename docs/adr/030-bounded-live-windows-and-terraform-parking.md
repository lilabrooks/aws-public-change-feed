# ADR-030: Bounded live windows and Terraform parking

- Status: Accepted
- Date: 2026-09-07
- Owner: Lila Brooks
- Revision accepted: 2026-09-08

## Context

Dev is used for short live tests. Disabling scheduled acquisition alone leaves
queue polling, alarms, the dashboard, and direct invocation available. A local
timer cannot shut these down after a terminal closes or an SSO session expires.
The implementation must preserve application packages and retained data.

## Decision

Add explicit central Terraform phases and a separate `infra/live-control` root.
The operator commands share one Python controller. A Step Functions Standard
execution owns each window, including activation failures and shutdown. CodeBuild
runs only activation, the delivery protocol when selected, and parking. An AWS
SDK DynamoDB callback task waits for an early-stop request or the original
deadline without a polling loop or an idle build container.

The DynamoDB control item uses a conditional generation claim. It has no TTL.
The execution name is stored before submission, so an uncertain submission is
retried with the same name and input. No new generation starts while the previous
owner is running or its controls are not verified parked. Early park sets a stop
flag and returns the registered callback token. Conditional token registration
handles a stop arriving before the token exists. It never cancels the cleanup
owner. Failed parking receives two fresh-build retries, then remains visible for
`live-park` recovery with the same immutable bundle.

Each bundle contains one clean Git revision and complete private Terraform
inputs. Its S3 key contains its digest and every build pins the S3 VersionId.
Build output excludes private inputs and Terraform plan text. Fresh saved plans
are restricted to trigger state, execution concurrency, and monitoring lifecycle;
package changes, IAM changes, data deletion, and unexpected output changes fail
closed. The bootstrap backend policy gains one exact control-state key.

The runtime-failure queue policy uses local JSON rendering. A provider policy
document read was deferred whenever a referenced schedule changed state, making
the unchanged queue policy unknown and blocking the toggle. Local rendering
keeps the exact rule ARNs and account restriction known. It preserves conditional
watcher/dispatcher grants and the reconciler grant; policy changes remain outside
the controller's authority. Fresh plans must show that policy unchanged.

The lifecycle validator permits unknown values only for the pinned central
provider's computed-only Lambda/mapping attributes. Unknown configurable
fields remain refusals, including nested unknowns; real transition plans are
still a first-use evidence gate. Controller concurrency follows the bundled
deployment policy. The exact mapping UUID remains coupled to its IAM grant and
requires live comparison after replacement.

AWS can return disabled mapping metrics as an empty block. Provider 6.58.0
preserves that block on refresh but rejects a configured empty metrics set.
The worker mapping ignores changes to this representation only alongside a
Terraform postcondition requiring every observed metrics set to be empty in
legacy mode and every phase except `stopping` and `parked`. Those two shutdown
phases must be able to fence execution and remove monitoring despite this drift.
Controller read-back still rejects enabled metrics and unexpected shapes after
the shutdown applies, leaving the operation failed until separately reviewed
recovery. The lifecycle field allowlist is unchanged; parking does not repair
metrics drift. Other plan, permission, provider, and lock failures can still
prevent shutdown.

The build role can refresh the central Terraform state and mutate its narrow
control surface. Delivery preflight additionally needs exact invocation and
queue-receipt permissions. It cannot read Slack secret values, alter runtime IAM,
write application packages, or delete retained tables, queues, log groups, or
buckets. The role is an operator deployment control, not a new product adapter
or customer-account integration.

## Lifecycle and failure boundaries

The [operator runbook](../runbooks/live-window.md) defines the phase table and
commands. Parking fences acquisition first and drains only consumers already
active. It allows the watcher timeout, a bounded drain, then fences all five
functions and allows the final invocation timeout. Evidence failures must not
prevent an attempt to fence execution and remove monitoring. A failed drain
retains unresolved work; it never extends the window to obtain a passing sample.

An interrupted local `live-test` makes one best-effort early-park request.
Failure of that request prints only a fixed warning and still returns exit 130;
provider details cannot escape the interrupt handler. The durable cleanup owner
is never cancelled by that local interruption.

The existing asynchronous retry policies remain unchanged. Neither concurrency
zero nor elapsed function timeout proves Lambda's accepted asynchronous queue
empty. Unpark intentionally resumes retained application work and may execute
previously accepted async events. The controller reports this boundary and does
not purge queues or replay `delivery_unknown`. This replaces the earlier
unimplemented proposal to change retry policy and impose an async cooldown;
there is no documented queue-empty proof or hard propagation bound on which to
base that stronger claim. Direct delivery preflight keeps its existing fresh
state and exact-input refusals and is never retried automatically.

`WINDOW` is an operational target including setup and cleanup, not a guaranteed
AWS billing cutoff. One reviewed timing file supplies the controller and control
Terraform root. It reserves 47 minutes for one cleanup attempt: a 40-minute
build limit, five-minute queue limit, and assumed two-minute provisioning margin.
The planned work is 25 minutes, including both invocation boundaries and drain;
its control-work allowance is not a live measurement. Two emergency retries and
their backoff can exceed the ordinary reserve. The three-attempt allowance is
142m30s; adding the bounded failure-notification task gives 143 minutes to the
terminal-failure allowance, still subject to AWS delays and the orphan-lock
boundary.

Initial setup has ten minutes, with another ten reserved for remaining
activation work after prepared. The local readiness wait uses their twenty-minute
sum. After prepared, activation must have its allowance plus usable case time;
usable time is checked again after activation. These are unmeasured planning
allowances. Minimum windows are 68 minutes for manual/observation and 78 minutes
for delivery; the maximum remains 364 days. Early completion or
an early-park request starts cleanup without waiting out the reserve. Long
windows do not increase workflow history through polling. Indefinite sessions
are not supported. The runbook carries the budget breakdown and proof sequence.

## Costs and retained resources

Parked mode removes all 28 metric alarms and the operations dashboard. All three
rules and the worker mapping remain disabled, with all five functions at zero
reserved concurrency. Provisioned Lambda capacity or pollers cause refusal.
After in-flight work finishes, ordinary runtime execution, polling, and resulting
log/metric ingestion stop. This is not a zero-bill configuration.

CloudWatch log storage, Secrets Manager, DynamoDB storage/PITR, S3, and control
storage remain. Monitoring savings depend on shared free allowances and account
usage. Stopping custom-metric publishing also reduces metered runtime activity.
Control builds and transitions cost when used, and frequent sessions can offset
monitoring savings. The September 8 deadline proof measured activation and
parking build wall times; it did not measure net savings or worst-case costs.
The control log group retains 14 days; application retention is unchanged.
Successful, resolved sessions become eligible for exact-version bundle/evidence
retirement 90 days after a verified closeout. `live-prune` remains a separate
preview/hash/apply operation, never part of mandatory parking. Active owners,
failed or unresolved sessions, and unproven orphan uploads stay protected.
Closeouts persist beyond AWS execution-history retention and remain after
pruning as the retry inventory. The ownership ledger has no TTL. The only
age-only bucket lifecycle action aborts incomplete multipart uploads after one
day; it cannot reclaim completed orphan uploads. No application data, packages,
releases, secrets, or PITR policy changes are included.

## Failure notification and closure

Two retained EventBridge event-pattern rules route failed/aborted/timed-out
workflow executions and failed/faulted/stopped/timed-out control builds to the
existing operations SNS email topic. They do not run on a schedule and do not
reintroduce metric alarms. A dedicated target role grants only `sns:Publish` on
that topic, with assumption restricted to this account and these two rule ARNs.
Central remains the sole owner of the SNS topic policy. Notifications contain
only status, execution/build identity, and console guidance, not event inputs,
build environment values, or failure payloads.

After exhausted parking retries, the workflow also makes one bounded SNS
publish attempt, then fails whether publishing succeeds or fails. This route
can duplicate the terminal event notification and shares its SNS dependency;
it is not an independent delivery guarantee. Build alerts are early warnings,
not proof that cleanup ultimately failed. Event delivery is best effort. Prove
receipt with the actual confirmed subscription before unattended use.

### Supervised-use qualification

The owner accepted supervised short dev windows on 2026-09-08. An operator
must remain available until the workflow and builds are terminal and independent
readback confirms parked controls, removed monitoring, and classified retained
work. Failed or uncertain cleanup requires operator recovery. The AWS owner
still performs deadline cleanup when the local waiter is lost; this qualification
does not permit leaving a window unattended.

Reuse the confirmed build-failure email while the destination and mechanism
remain unchanged. A terminal-failure email was also confirmed, but its route
was not identified. Do not mark either the workflow-event or direct
exhausted-cleanup route separately receipt-qualified. Failure preservation when
the direct publish fails remains unproved live. The source and offline checks
remain required, with their evidence kept distinct from live results.

[L-59](https://github.com/lilabrooks/aws-public-change-feed/issues/205) gates
unattended use on the remaining route-specific receipt and live failed-publish
qualification. Inspect existing emails first; test only a still-missing path
under a separately approved safe procedure. This keeps deliberate cleanup
failure and changes to a shared notification dependency outside ordinary live
testing. The alternative, completing the whole fault-injection campaign now,
would add cost and operational risk without changing the supervised decision.

Reassess affected evidence when the workflow, provider, control IAM, mapping
identity, tags, or timing policy changes. Changes to the topic, subscription,
notification routes or their permissions, or evidence of missed notifications,
also require reassessment before relying on earlier receipt proof. Fresh plans,
exact application/release checks, valid credentials, and per-use readbacks
remain mandatory. Notification mechanisms, retention, IAM, and timing reserves
are unchanged by this acceptance revision.

The local waiter attempts closeout after verifying terminal success and parked
controls. A subsequent unpark tries again before replacing the old ledger.
`live-close` supplies the same explicit operation. Closeout requires exact owner
input, successful terminal execution, complete evidence, converged parking,
zero recorded unresolved delivery counts, and zero approximate queue counts.
These counts do not establish an empty Lambda async backlog. Recovered build
failures may close when the session itself finishes successfully. An absent
waiter does not weaken cleanup, but absent/expired closure evidence retains
objects indefinitely until separately reviewed. A closeout failure never
blocks parking or silently grants deletion authority.

## Alternatives and rollback

A local `finally` block was rejected as the only cleanup owner. Scheduler calling
CodeBuild directly was rejected because accepted submission does not retry a
subsequently failed build. Keeping CodeBuild running throughout a window would
bill idle time. Repeated workflow polling risks the execution-history limit.
The callback task avoids both costs and history growth while retaining one owner.

Initial adoption requires the accepted trust decision, reviewed saved plans
and IAM, and a bounded round trip plus loss of the local waiter. Those lifecycle
gates now have the supervised evidence below; they do not establish a shutdown
guarantee under every AWS failure or qualify unattended use.
Rollback first verifies the application parked and every owner terminal. Restore
legacy operator controls explicitly with `live_mode=null` only under a reviewed
plan. Never remove the control root while it owns a live session.

## Verification

AWS `ValidateStateMachineDefinition` accepted the corrected Terraform-rendered
definition on 2026-09-07 with `result=OK`, no diagnostics at `WARNING` severity,
and `truncated=false`. The initial submission rejected `BOOL`; the SDK
integration requires `Bool`. The runbook records the validated definition hash,
including the exhausted-cleanup SNS state. That definition was deployed with
the 21 owner-approved control resources on 2026-09-07. The initial central
parked migration removed 28 alarms and the dashboard, fenced all five functions,
and preserved retained resources. Post-wait read-back and fresh no-change plans
completed at 17:45:57 UTC. The runbook records the source snapshot and limits.

The owner approved the exact initial plans and their control IAM for deployment.
The owner explicitly accepted the ongoing trust decision on 2026-09-07.
Acceptance does not qualify unattended operation. Deployment and syntax
validation alone did not prove build-role execution, notification receipt, or
the shutdown lifecycle. Later source fixes need a fresh reviewed bundle and
transition plans; they are not covered by that deployment.

The supervised attempt exposed two missing S3 tag reads. Each scoped IAM repair
received separate owner approval. After a complete read-only plan passed under
the build role, parking-only recovery succeeded on September 8 UTC. It recreated
21 tagged baseline alarms and removed them after the required invocation wait,
then proved parked convergence. Build and terminal-failure emails were received.
The [runbook](../runbooks/live-window.md#first-supervised-attempt-2026-09-07)
records the failed activation, retries, grants, and successful recovery without
treating that recovery as a successful live test.

A later supervised attempt failed during rule updates because the tagged
`PutRule` request required `events:TagResource`. The pinned provider sends
tags even for a state update. Failure cleanup succeeded and preserved the
activation failure as the workflow result; that attempt did not prove unpark/early-park.
The source repair limits the companion permission to the three exact runtime
rules and the four fixed request-tag values, with no extra keys or tag-removal
grant. Stored tags do not select the target or confer access. The same
restriction also limits a direct tag request to the fixed payload.

An ARN-only tag grant would permit arbitrary tag values on those rules. The
request restriction avoids that broader capability but requires separate
permission review if supplementary rule tags are introduced. Changing the
provider or bypassing Terraform's rule updates would add a different lifecycle
path. Keep the existing controller and plan validator unchanged. Review a
parked maintenance plan before applying the IAM repair, then repeat supervised
qualification from clean source. If the request conditions fail live, park and
inspect the denial before changing the grant. Removing the companion statement
is a separate parked rollback; it restores the known rule-update refusal.
The separately reviewed parked IAM apply completed. Policy readback matched
the constrained grant and a fresh control plan had no changes. The subsequent
supervised unpark/early-park passed from clean source. CloudTrail confirmed
tagged updates in both directions on all three rules and mapping requests
without a metrics payload. Both builds and the same-owner workflow succeeded.
Independent readback and no-change plans confirmed parking and preserved
identities and rule tags. Eligible closeout passed without deleting evidence.
That manual round trip did not itself prove deadline cleanup or the separate
fixed observation. The later
[September 8 deadline proof](../evidence/l57-l58-supervised-live-window-2026-09-08.md)
passed both on clean source `5aed8a6e67b0e34be1ef94a40b6ad3c470169bbd`.
The predeclared 20-minute cohort had the expected 1/20/4 scheduled invocations
and heartbeats, zero scheduled-function errors/throttles, and no newly posted
delivery. It ended without extension. After local waiter loss, the unchanged
owner and deadline reached normal parking without an early-stop request.
Both builds and the workflow succeeded, invocation boundaries were observed,
monitoring was removed, and fresh central/control plans converged. Eligible
closeout passed with objects retained. Detailed receipts remain private.

The lifecycle is qualified for supervised use within the stated limits.
Per-use review and changed-mechanism requalification remain required.
L-59 holds the unperformed notification checks before unattended use; they are
deferred, not passed. No new live fault injection or retention change follows
from this revision, and measured samples do not establish worst-case budgets.

References verified: 2026-09-07.

- [Step Functions integration and callback semantics](https://docs.aws.amazon.com/step-functions/latest/dg/connect-to-resource.html)
- [Step Functions JSONata](https://docs.aws.amazon.com/step-functions/latest/dg/transforming-data.html)
- [CodeBuild integration](https://docs.aws.amazon.com/step-functions/latest/dg/connect-codebuild.html)
- [Lambda asynchronous error handling](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-error-handling.html)
- [Lambda concurrency](https://docs.aws.amazon.com/lambda/latest/dg/configuration-concurrency.html)
- [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/)
