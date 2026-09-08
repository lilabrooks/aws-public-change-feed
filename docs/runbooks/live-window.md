# Short live windows and parking

Implementation for M4 / L-58, governed by accepted
[ADR-030](../adr/030-bounded-live-windows-and-terraform-parking.md). Initial
parked migration and control-plane deployment completed on 2026-09-07. The
live round trip remains unproved; build and terminal-failure emails have been
received. Complete the
first-use gate below before relying on automatic shutdown.

## Repeatable commands

Set `LIVE_CONFIG` once to a private JSON file outside the repository, then use:

```sh
make live-unpark WINDOW=90m
make live-test CASE=delivery WINDOW=90m
make live-test WINDOW=90m
make live-park
make live-status
```

`live-unpark` waits up to twenty minutes for verified readiness. A pending or failed
local wait does not cancel AWS cleanup. Run `live-status` for the current state.
Repeated unpark reports the original session; it never extends the deadline.
The command prints that unpark resumes retained work, including possibly queued
asynchronous invocations, before reporting readiness.

`live-test` defaults to observation when `CASE` is omitted or empty and waits for
the selected test and parking. Delivery reuses the existing
preview/hash/apply protocol with automatic triggers off and one attempt only.
It ends on a post, no positive match, refusal, or ambiguous outcome. Refusals
retain their status and detail in private evidence and set the recorded outcome
and stop flag before the build fails into cleanup. A quiet
sample is not extended. An operator still confirms the Slack destination as
required by that protocol. Observation runs the ordinary schedules for the
bounded window and captures control/retained-work evidence; its completion is
not proof that a matching announcement arrived or that every runtime criterion
passed. Synthetic recovery and load still use the isolated preflight procedures.

Ctrl-C during `live-test` makes one best-effort early-park request and exits 130.
If that request fails, the local command prints a fixed warning without provider
details. The AWS cleanup owner remains responsible; check `live-status` when
your operator session is usable again. Interrupting `live-unpark` only ends its
local wait and does not request early parking.

`live-park` requests early cleanup without cancelling its durable owner. It waits
for the outcome. A second park of a verified parked service makes no changes.
If the owner failed, the same command starts parking-only recovery with the
original bundle. Never delete the control item or force-unlock Terraform merely
because a deadline elapsed. An abandoned Terraform lock requires checking its
actual build owner before a separately reviewed recovery action.

The local command timeout first interrupts Terraform gracefully to let it save
state and release its lock. A container failure or final forced termination may
still leave an orphan lock. Cleanup retries cannot bypass that lock. This is an
explicit manual-recovery boundary, not a claim that every interrupted apply
recovers automatically; verify the prior build terminal and review the exact
lock before unlocking.

`live-status` makes bounded reads of controls, monitoring, retained delivery
states and queues. It also reports the durable owner/deadline and residual cost
categories. It excludes callback tokens and credential values. It does not
enable resources or monitor continuously.

## Window meaning

Use ordered `d`, `h`, `m`, `s` components, for example `90m`, `2h`, or `1h30m`.
The minimum is 68 minutes for manual/observation and 78 minutes for delivery.
The maximum is 364 days, leaving room within the Standard workflow lifetime for
cleanup. Invalid, negative, duplicate-unit, indefinite, or too-short windows
fail before activation. The reviewed
[`live_window_budget.json`](../../scripts/live_window_budget.json) supplies the
Python controller and the control Terraform root with the same timing inputs:

| Budget | Current value | Meaning |
| --- | --- | --- |
| Planned cleanup work | 25 minutes | Two 300-second invocation boundaries, up to 120 seconds of drain, and a 13-minute allowance for installation, Terraform, verification, and evidence |
| Build timeout | 40 minutes | Configured CodeBuild execution limit, with room beyond planned work |
| Cleanup reserve | 47 minutes | One 40-minute build, five-minute queue limit, and assumed two-minute provisioning allowance |
| Initial setup reserve | 10 minutes | Queue/provisioning, installation, initialization, and prepared phase allowance |
| Remaining activation allowance | 10 minutes | Remaining direct or draining/live Terraform cycles and read-back after prepared |
| Total readiness allowance | 20 minutes | Initial setup plus remaining activation; also used by the local readiness wait |
| All parking attempts | 142m30s | Three 47-minute allowances plus 30- and 60-second retry delays |
| Terminal cleanup failure | 143 minutes | All parking attempts plus the bounded 30-second notification task |

The planned workload and provisioning allowance have not been measured live.
The full retry allowance is **outside** the ordinary one-attempt cleanup reserve;
emergency retries may run past the requested deadline. AWS provisioning, API
delays, and orphan locks can exceed these allowances. No command guarantees an
exact last billable second. A timeout increase alone does not make lock recovery
automatic.

The minimum includes both setup/activation allowances, one cleanup attempt, and
one minute of observation or the delivery protocol's eleven minutes. After
prepared, the controller requires the remaining activation allowance plus the
usable observation/protocol time. It rechecks usable time after activation,
before declaring readiness or starting delivery. An overrun refuses and enters
cleanup; these allowances do not guarantee the work will fit. They add no
sleeps. Early park and completed delivery start cleanup promptly; CodeBuild exits
when its work finishes. Shorter windows require reviewed budget changes supported
by live timings, not an operator override that removes cleanup time.

## Terraform phases

| Phase | Automatic triggers | Execution | Monitoring |
| --- | --- | --- | --- |
| `parked` | All off | All five fenced | All alarms and dashboard absent |
| `prepared` | All off | All five fenced | Baseline 21 alarms and dashboard |
| `live` | All four on | Four durable executors; shadow fenced | All 28 alarms and dashboard |
| `direct` | All off | Four durable executors; shadow fenced | Baseline monitoring |
| `draining` | Acquisition off; consumers on | Watcher and shadow fenced | 24 eligible alarms and dashboard |
| `stopping` | All off | All five fenced | Baseline monitoring until removal |
| `shadow` | All off | Shadow only | Baseline monitoring |

`shadow` is a Terraform phase, not a registered automatic test. Its existing
operator protocol remains separate. `live_mode=null` preserves historical
rollout, preflight, and recovery behavior; those procedures must not overlap a
live-control session. After adoption, complete private central inputs must keep
`live_mode=parked`. Ordinary package deployments also use those parked inputs.
Do not apply central Terraform independently while a live window owns it.

Shutdown never enables an inactive consumer just to drain it. Partial/direct
sessions may therefore leave accepted work for a later reviewed unpark. Existing
async retry policy and log retention are unchanged. A parked receipt is not a
claim that Lambda's asynchronous queue is empty. Failed, retryable, queued, or
unknown delivery work remains in its system of record, not purged.

## First-use gate and private configuration

1. Review/accept ADR-030 and the control IAM, including read access needed by
   Terraform refresh and the extra exact delivery-preflight permissions. The
   build role includes `s3:GetReplicationConfiguration` and
   `s3:ListTagsForResource` only on `apcf-config-dev`: the pinned provider reads
   replication configuration and bucket tags during refresh. The tag read does
   not grant tag mutation. Artifact refresh also needs `s3:GetObjectTagging`
   restricted to `apcf-config-dev/apcf/application-artifacts/*`, with no
   object-tag write permission. This grant follows the locked central provider's
   observed tag-read call, although both artifact data sources pin object
   versions. Recheck that call and the scoped permissions when upgrading the
   provider; `s3:GetObjectVersionTagging` has not been shown necessary by the
   current live proof and is not granted speculatively.
   Schedule state updates also carry the pinned provider's tag payload and
   need the companion `events:TagResource` permission. The source repair grants
   it only on the three exact runtime rules, requires all four fixed runtime
   request-tag values, rejects extra keys, and grants no `UntagResource`.
   Check the real rule tags and planned request payload against those conditions
   before applying this repair or starting another window. Supplementary rule
   tags require separate permission review; never remove them to make a toggle
   pass. IAM simulation is read-only evidence, not proof that the provider's
   dependent authorization will pass live. The separately reviewed parked IAM
   apply completed with matching policy readback and a no-change control plan;
   live provider qualification remains pending.
   The mapping UUID is a coupled constant in `scripts/live_window.py` and the
   `ExactQueueTrigger` policy in `infra/live-control/iam.tf`. Compare both with
   the deployed central mapping before first use and after any replacement.
   A replacement requires coordinated controller/IAM review; the local pairing
   test cannot detect live UUID drift. Worker concurrency is read from the
   bundled `infra/central/deployment.yaml`, as it is by central Terraform.
2. Run local checks, render the actual state-machine definition, and submit it
   to AWS `ValidateStateMachineDefinition`. On 2026-09-07, AWS returned `OK`,
   no diagnostics at `WARNING` severity, and `truncated=false` for the corrected
   rendered definition. Its SHA-256 is
   `aa79a85427c7911bbf4288a66fc79afdf860b67a35b8d32e05861c7f037da76e`.
   The initial submission rejected `BOOL`; this SDK integration requires `Bool`.
   This hash includes the exhausted-cleanup SNS state and matches the deployed
   definition recorded in the 2026-09-07 deployment receipt. Repeat validation
   after definition changes. Syntax validation alone does not prove runtime
   permissions or the shutdown lifecycle.
3. Preserve the complete existing private central inputs: every artifact digest,
   VersionId and checksum, notification endpoint, and other non-default setting.
   Set `live_mode=parked`, disable both legacy trigger booleans, clear individual
   overrides, and keep `watcher_execution_paused=false`. Recovery/preflight modes
   cannot be combined with managed live phases. Never rebuild inputs from only
   public Terraform outputs, which omit private subscription values.
4. Review fresh saved plans for the bootstrap backend-policy extension, the
   separate `infra/live-control` root, and central parked migration. The central
   migration adds outputs and explicit monitoring address moves, disables the
   exact rules/mapping, fences five functions, and removes monitoring. Verify
   packages, IAM, retained data, logs, secrets, queue settings, and PITR unchanged.
   Apply only after the exact plans are authorized. The routine controller
   deliberately refuses to perform this migration itself.
   Inspect `after_unknown` before the first parking apply. The controller
   exempts only the 17 computed-only attributes verified against the pinned
   central provider schema, and only when explicitly unknown. Configurable
   unknowns and known changes outside the permitted control fields still fail
   closed. Real prepared-to-draining and live-to-stopping saved plans must be
   checked before their respective applies during supervised qualification.
   A plan from already-parked state does not prove a live-to-stopping transition;
   record any missing transition evidence as an open gate. Keep plans private.
   AWS may report disabled mapping metrics as `MetricsConfig: {Metrics: []}`.
   Provider 6.58.0 preserves that empty block but rejects configuring `metrics=[]`.
   The mapping therefore ignores `metrics_config` representation drift with a
   postcondition requiring every observed metrics set to be empty except in
   `stopping` and `parked`. Enabled-metrics drift therefore refuses activation
   and optional draining, but permits the mandatory shutdown plans to fence
   execution and remove monitoring. Controller verification then rejects the
   drift after those applies. Unexpected API metrics shapes also fail read-back.
   The plan allowlist still refuses metrics changes; parking does not repair
   metrics drift. Keep the operation failed, inspect the actual controls, and
   review metrics recovery separately before retrying. Other plan, permission,
   provider, and lock failures can still prevent shutdown. After approved
   recovery, regenerate the plan and repeat parked read-back.
   The runtime-failure queue policy is rendered locally to avoid a deferred
   provider data read when schedule states change. Compare its decoded policy
   with the retained policy and require a no-op in trigger-changing plans.
   Do not permit policy mutations or ignore policy drift to make a toggle pass.
5. Read back all stopped controls and wait the maximum function timeout. Save
   a private configuration with these exact keys (replace the two local paths):

   ```json
   {
     "account": "667653114001",
     "region": "us-east-1",
     "bucket": "apcf-dev-live-control-667653114001",
     "table": "apcf-dev-live-control",
     "project": "apcf-dev-live-control",
     "state_machine": "arn:aws:states:us-east-1:667653114001:stateMachine:apcf-dev-live-control",
     "tfvars": "/absolute/private/path/central.tfvars.json",
     "profile": "aws-public-change-feed"
   }
   ```

   `profile` is optional if normal AWS credential resolution is configured.
   This file contains no credentials. The baseline tfvars file is private and
   is copied into the encrypted, versioned control bundle. Keep it outside Git.
   Export `LIVE_CONFIG=/absolute/private/path/live.json` in your shell profile.
6. Use a reviewed clean commit for the bundle. Complete a short manual
   unpark/early-park round trip, then a deadline run with the local waiter
   terminated. For that deadline run, start `make live-test WINDOW=90m` and
   verify readiness with `make live-status` from a separate terminal before
   terminating only its verified local Python
   waiter with `SIGKILL`. Ctrl-C/`SIGINT` enters the early-park handler and
   cannot prove deadline cleanup. Do not stop an AWS build or workflow, kill
   unrelated processes, or send `live-park` during the deadline proof. Record
   the owner and timing fields before and after waiter loss; confirm the same
   generation, execution, deadline, and `cleanup_at`, with no early stop
   requested. A human must remain present to observe cleanup and handle a
   failure; losing the waiter does not end supervision.
   Verify recreation and removal, both invocation boundaries,
   preserved identities, explicit unresolved work, and a converged parked plan.
   Exercise a failed build/parking retry without manufacturing Slack traffic.
   Do not mark the automatic cleanup guarantee live-qualified before this proof.
   Use `WINDOW=90m` for the first supervised proof; this supplies unmeasured
   setup margin, not a reason to keep a completed test running. Earlier 58/68
   minute minimums omitted remaining activation work. Capture actual phase
   timings and revise allowances only from that evidence. A quiet sample must
   not be extended to obtain a match. Check retained runtime-failure
   queue contents around stopping; concurrent schedule/fence changes may produce
   late events. Do not purge work to make a proof pass.
   The watcher retains a 900-second maximum async event age, versus the final
   300-second invocation wait (dispatcher/reconciler event age is 300 seconds).
   A zero-count receipt can precede late runtime-failure queue arrivals; alarm
   removal and a closeout do not establish that async backlog is empty.

### Initial deployment record, 2026-09-07

The approved source snapshot was `2aa99933d0a0c07219e9a316dd601073dff2dbdf`.
Its central plan removed 28 alarms and the dashboard and fenced all five
functions. The three schedules and queue mapping were already disabled and
remained so; this apply did not prove the enabled-to-disabled direction.
The separate control root created 21 resources. The bootstrap apply changed its
backend-policy output only; it did not attach a policy to an IAM principal.

Final verification completed at 17:45:57 UTC after waiting more than the
300-second invocation boundary. Direct read-back confirmed parked controls,
unchanged application identities, and absent provisioned capacity. Fresh plans
for all three roots had no changes. The deployed state-machine definition
passed AWS validation with the hash in step 2. The restricted deployment
receipt retains approved plan hashes, apply results, and remote state versions.

At that verification there were no control builds, workflow executions, or
ownership item. Delivery records and queues were not inventoried; empty receipt
placeholders do not prove empty work. Source fixes made after that snapshot
need a newly reviewed clean bundle and fresh transition plans before live proof.
Never reuse the initial migration plans for qualification.

At initial deployment, failure notification resources were deployed but actual
receipt was unproved. Later receipt evidence is recorded below.
Keep a human supervising initial qualification, including the local-waiter-loss
test, and do not use routine unattended windows until actual email receipt and
the cleanup lifecycle are proved.

### First supervised attempt, 2026-09-07

The owner accepted ADR-030 and authorized a supervised window from reviewed
clean source. Activation failed before applying runtime or monitoring changes.
Terraform refresh exposed missing bucket-tag read permission; parking-only
recovery then exposed a separate artifact object-tag read requirement. Each
scoped IAM repair received separate owner approval. An initial diagnostic's
Terraform plan failed; after both repairs, a complete read-only plan passed
under the actual build role.

Parking-only recovery succeeded on September 8 UTC using the original
generation and immutable bundle. It recreated the 21 baseline alarms with
verified shared tags while all runtime controls remained fenced. After the
required 300-second invocation wait, it removed monitoring and passed full
parked convergence. Independent readback confirmed disabled runtime triggers,
zero reserved concurrency on all five functions, zero alarms, no dashboard,
and terminal control builds and workflows. Recorded deliveries were posted,
and all queues reported zero approximate counts; those counts do not prove
Lambda's asynchronous backlog empty.

The result was `parked` with outcome `activation_incomplete` and
`retained_without_closeout`. Failed activation and diagnostic evidence remain
protected. This proves parking-only recovery and baseline alarm recreation and
removal. Live unpark, the full 28-alarm set, early parking from live state,
deadline cleanup after local waiter loss, and eligible successful-test closeout
remain unproved. No new live window followed the repair.

The owner confirmed build-failure and terminal-failure emails. The terminal
confirmation did not distinguish the workflow-event email from the direct
exhausted-cleanup email. Direct publication succeeded; separate receipt
confirmation for each terminal route and failure preservation when publication
fails remain open. Inspect the existing emails before considering separately
authorized fault injection.

Detailed execution IDs, exact plan hashes and timestamps, state versions,
diagnostic output, and readback receipts stay in restricted operator evidence.
Retain both failing and passing attempts there. Public documentation records
the outcome and its limits; it is insufficient input for an apply or recovery.
Before another live window, review the exact private receipts, current controls,
and fresh plans against the first-use gate.

### Later supervised attempt, 2026-09-08 UTC

Fresh full central and live-control plans reported no changes before the next
authorized attempt. Activation recreated monitoring and partially enabled
consumers, but rule updates failed. CloudTrail recorded `PutRule` denials for
missing `events:TagResource`. The watcher stayed fenced and all three schedules
remained disabled. An early-stop request preserved the same cleanup owner.

Failure cleanup disabled the queue mapping, fenced all five functions, waited
the required invocation interval, and removed the remaining alarms/dashboard.
The cleanup build succeeded while the workflow correctly ended
`LiveWindowFailed`. Independent readback and a full no-change central plan
confirmed parking, preserved application and resource identities, and zero
recorded unresolved delivery work. Approximate queue counts were zero; that
does not establish an empty asynchronous backlog. Failed evidence stays
retained without successful-test closeout. No replacement window was started.

The narrowly scoped permission repair was then applied through a separately
reviewed parked IAM plan. Only the constrained build-role statement changed;
policy readback matched and a fresh control plan had no changes. Runtime
controls remained parked. Live qualification is still required. Keep detailed receipts and
timings in restricted evidence. A read-only plan did not exercise the missing
write permission, so another no-change plan cannot by itself close this gate.

## Resource tags and billing activation

Terraform defines four shared tags on every resource supported by the locked
provider. The [September 7, 2026 maintenance apply](#tag-deployment-record-2026-09-07)
verified tags on 47 existing resources. Subsequent Billing readback returned
`Active` for `project` and `deployment_id`. At the last recorded check on
September 8 UTC, `component` was absent from Billing's discoverable keys.
Its activation and eventual cost attribution remain pending; keep the service
parked during that discovery delay.

| Key | Values and purpose |
| --- | --- |
| `project` | `aws-public-change-feed`, including bootstrap and live-control |
| `deployment_id` | The actual deployment, including `preflight` for isolated resources |
| `managed_by` | `terraform` |
| `component` | `runtime`, `storage`, `monitoring`, or `live-control` |

Central functions, rules, the queue mapping, and IAM use `runtime`. Buckets,
tables, queues, and secret containers use `storage`. Logs, alarms, and the
operations topic use `monitoring`. Bootstrap uses `storage`; all live-control
resources use `live-control`, so its storage and execution overhead stay
together. These are ownership groups, not billing or retention promises.
Logs still incur storage usage when their `monitoring` component is parked.
Supplementary tags are preserved, including preflight's `lifecycle` and the
control root's `purpose`; the four shared keys are fixed by Terraform.

The Slack queue mapping is tagged explicitly because it does not inherit
function tags. Alarms inherit the shared monitoring tags whenever recreated.
Central's locked provider (6.58.0) does not expose dashboard tags. Keep the exact
dashboard name and Terraform address as its inventory identity. Event targets, Lambda invoke
configuration and permissions, inline IAM policies, SNS subscriptions/policies,
and separate S3/SQS configuration resources also have no independent `tags`
field in these schemas. Their owning rules, functions, identities, topics,
buckets, and queues carry tags. Tests recheck the exceptions against the
provider locks. This list describes Terraform support, not every current AWS
API capability; a provider upgrade needs its own review.

Apply tag changes only through this maintenance sequence:

1. Run `live-status` and verify parked controls and the reported owner. Check
   Step Functions for running owners and CodeBuild for active control builds
   separately; `live-status` does not list builds. Finish any failed owner's
   recovery before changing tags, even if the runtime already appears stopped.
   Check account-level tag-based access policies for these keys; a tag-only
   diff cannot prove unchanged effective access under policies outside this repository.
2. Preserve complete private tfvars and review fresh plans for bootstrap,
   central, and live-control. Central inputs stay `live_mode=parked`. Require
   only in-place `tags`/`tags_all` changes, no replacements, and no changes to
   packages, controls, IAM policies, retention, secrets, or stored data. Do not
   create absent preflight resources just to tag them. If an isolated
   deployment exists, review it separately while its exercise is inactive.
   Full plans can defer unchanged policy-document reads when a referenced
   resource's tags change. Resulting unknown policy updates are not approved
   tag changes. If encountered, stop and review the staged exception below.
3. After exact plan approval, apply those plans, read back the tags and parked
   controls, and require fresh no-change plans. Keep absent alarms/dashboard
   absent; do not enable the service to demonstrate tags.
4. Use the newly reviewed clean source for the next control bundle. Finish
   recovery with an old immutable bundle before maintenance; an old bundle's
   tag expectations can otherwise produce drift and block shutdown. Routine
   toggles still reject tag changes. Do not widen their allowlist or add tag
   mutation grants to make maintenance fit inside a live window.
5. In the billing account's **Billing and Cost Management → Cost allocation
   tags → User-defined tags**, activate `project`, `deployment_id`, and
   `component`. Keep `managed_by` available for inventory without activating
   it for billing unless that grouping becomes useful. This account-level
   action is separate from Terraform deployment. It requires the organization's
   management account or an eligible standalone account.
6. Allow up to 24 hours for new keys to appear, then up to another 24 hours for
   activation. Verify their active status and, once billing data arrives,
   inspect Cost Explorer filtered to this project and grouped by deployment
   or component. Compare with service/account totals and inspect unattributed
   charges. A taggable resource does not guarantee all related usage carries
   its tags. Short live windows may not yet appear in billing data.

Tags remain static during park/unpark. Never infer shutdown from a tag or use
tag selection for bulk deletion. Do not add per-window identifiers as metric
dimensions, scheduled tag checks, or cost-export infrastructure for this
workflow. Existing direct read-back remains the shutdown proof, and storage
retirement remains separately authorized. Keep credentials, private endpoints,
and customer data out of tags.

### One-time staged maintenance exception

Read-only previews on September 7, 2026 found deferred policy reads in the
bootstrap root and in central Lambda dependencies. Targeting every tagged
resource together still pulled central IAM policy updates into the plan.
Do not apply either of those plans as a tag-only change.

A separately reviewed targeted maintenance stage can exclude those downstream
dependencies. Record every exact target address and saved-plan hash. Bootstrap
targets its two buckets and concurrency-test IAM user. Central's first stage
targets the tagged storage, monitoring, IAM roles, and rules, with all Lambda
functions and the event-source mapping excluded. The control root's tagged
resources are reviewed separately. Do not create parked alarms or any other
absent resource just because its declaration supports tags.

After an approved first stage, regenerate a central plan targeting the five
existing functions and exact worker mapping. Review it anew: every mutation
must still be a tag-only in-place update, with unchanged execution controls.
If dependencies pull in an unknown policy update, stop for a new review.
After the remaining tags are applied, full untargeted plans for all affected
roots must report no changes before a new live bundle is eligible.

Targeted plans intentionally have incomplete coverage (`complete=false`). That
is an explicit exception for this separately approved maintenance sequence,
never permission to relax the live-window validator or skip final full-plan
convergence. Record partial progress if any stage fails; keep the service
parked and regenerate from actual state before resuming. No stage was applied
by the source-tagging change, and the later-stage/convergence proof remains
open until maintenance is authorized.

### Tag deployment record, 2026-09-07

After the tagging PR merged, the owner authorized a separate maintenance apply
from clean merged source with complete private inputs and the service parked.
The staged procedure above restricted changes to reviewed ownership tags and
preserved supplementary tags. Parked controls were checked after each apply.

Direct readback verified tags on 47 existing resources across bootstrap,
central, and live-control. Full untargeted plans for all three roots reported
no changes. Non-tag configuration, resource identities, and outputs were
preserved. No absent preflight resources, alarms, or dashboard were created,
and the runtime stayed fenced.

Restricted receipts retain the source revision, policy-inspection scope,
approved saved-plan hashes, before/after state versions, metadata differences,
and exact readbacks. Tagging did not run a live test or qualify automatic
cleanup. Billing activation is separate; its status is recorded above.
Later tag maintenance and the next live window still require fresh reviewed
inputs and the applicable first-use gates.

## Costs and failure receipts

Alarm/dashboard deletion removes those resources' future billable usage; actual
savings depend on shared free allowances and account usage. Disabled
queue mapping stops polling; zero reserved concurrency stops new executions,
not calls already running. Retained logs have storage costs even when no new
logs arrive. Secrets Manager, DynamoDB storage/PITR, S3, control ledger/bundles,
and control logs remain possible costs. Stopping runtime publishing also stops
new application custom-metric and log ingestion after in-flight work ends.
CodeBuild minutes and workflow/API requests accrue while operating, so frequent
round trips can offset monitoring savings. Session duration and net savings
have not been measured; do not treat the timing allowances as cost estimates.
Reserved concurrency itself is not provisioned
capacity. The controller refuses provisioned Lambda environments or pollers.

An evidence failure does not prevent mandatory shutdown attempts. Failed
activation/test and incomplete evidence remain distinct from parked controls.
A test with failed cleanup is a failed overall operation. Receipts record
unresolved delivery counts and deadline overruns. An S3 or provider outage can
prevent complete evidence or cleanup; inspect the failed owner and retry
`live-park` after resolving the cause. Retention changes or deletion of stored
application evidence, packages, secrets, and PITR require separate scope and
authorization. Control-bundle/evidence retirement follows the explicit procedure
below; it is never part of parking.

## Failure notifications

The control root retains two EventBridge event-pattern rules while central is
parked. Workflow failure/timeout/abort and control-build failure/fault/timeout/
stop publish minimal status and identity to the existing `apcf-operations` SNS
topic. Confirm its email subscription before use. A build alert is an early
warning: cleanup retries may still succeed. Use `make live-status` to distinguish
test failure, evidence failure, and unconfirmed parking.

Exhausted cleanup also attempts a direct SNS publish with a 30-second task
timeout before failing. A publish failure cannot turn the execution green.
Duplicate notifications are possible, and both routes depend on the same SNS
channel. AWS service events are best effort; these rules are not a delivery
guarantee. They add no scheduled invocations, permanent metric alarms, or polling
Lambda. Publication/delivery and retained storage may still incur usage charges.
The validated definition recorded in step 2 of the
[first-use gate](#first-use-gate-and-private-configuration) already includes
the exhausted-cleanup SNS state. Repeat service-side validation when the
definition changes.

The owner confirmed build and terminal-failure emails in the
[supervised attempt](#first-supervised-attempt-2026-09-07). The remaining receipt
check must distinguish the workflow-event and direct exhausted-cleanup routes;
inspect the received failure emails first. Successful workflow executions do
not exercise either terminal-failure route, and build-failure emails alone do
not distinguish them. Failure preservation when exhausted-cleanup
publishing fails also remains unproved live. Any deliberate fault injection
needs a separately reviewed and authorized procedure, including restoration
and verification of any changed configuration. It is not an implicit step in
the next normal live window.

Central is the only topic-policy owner. The EventBridge target role trusts only
the two exact rules in this account and may publish only to the operations
topic. The workflow receives the same exact-topic publish permission. Neither
route forwards private execution input, build environment values, or error data.

## Control retention: close, preview, then prune

Control build logs expire after 14 days. Completed control bundles and evidence
become eligible for retirement 90 days after verified successful session
closure, not 90 days after upload. Eligibility does not schedule deletion. The
ownership ledger has no TTL, and application retention/PITR are unchanged.
One-day multipart-abort lifecycle handles unfinished uploads only.

The local waiter attempts closeout on success; a later unpark tries again before
replacing the parked owner. You can explicitly capture the current closure:

```bash
make live-close
```

Closure requires a successful terminal owner with matching input, verified
parked controls, a converged complete receipt, zero unresolved delivery counts,
and zero approximate queue counts. A recovered build failure can qualify when
the session itself succeeds. These checks do not prove Lambda's async queue
empty. Closeout stores an immutable exact-version manifest in `closeouts/`.
Capture it before AWS execution history expires (normally 90 days); without it,
the objects remain retained. A closeout failure does not block parking or unpark.

When useful, preview retirement using a new private output path:

```bash
make live-prune PLAN=/absolute/private/path/control-prune.json
```

Review its exact keys and VersionIds, protected-session count, and SHA-256.
Deletion requires a separate explicit invocation with that hash:

```bash
make live-prune PLAN=/absolute/private/path/control-prune.json APPLY=<printed-sha256>
```

The preview expires after 24 hours. Apply rechecks immutable closeouts and
ownership, deletes only approved exact versions, and verifies their absence.
Current-session versions stay protected even after 90 days. Failed, unresolved,
and unclosed sessions are excluded. Losing-start or uncertain completed uploads
without proven closure are also retained; multipart lifecycle does not reclaim
them. Do not infer abandonment from absence in the latest ledger. They require
separate review, not a blanket bucket expiration rule.

Partial failure stops without reporting success. Keep the preview and retry the
same hash within 24 hours, or generate a new preview; already-absent exact
versions are safe to retry. Immutable closeouts remain as the retry inventory,
so a pruned session may appear again as already-absent targets. New unapproved
object versions remain untouched. This small retained metadata still uses S3
storage. No automatic deletion occurs during normal park/unpark.

Only the authenticated operator performs closeout/pruning. It needs control
bucket `s3:ListBucketVersions`, exact-object `s3:GetObjectVersion`, closeout-prefix
`s3:PutObject`, and, only for apply, `s3:DeleteObjectVersion` on `bundles/` and
`evidence/`. Existing identity, ledger, execution, and parked-control reads are
also required. The cleanup build role has no closeout-write or prune permission.
Keep that separation when reviewing operator access.

References verified: 2026-09-07.

- [EventBridge target permissions](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-use-resource-based.html)
- [EventBridge PutRule and tag authorization](https://docs.aws.amazon.com/eventbridge/latest/APIReference/API_PutRule.html)
- [EventBridge actions and request-tag condition keys](https://docs.aws.amazon.com/service-authorization/latest/reference/list_events.html)
- [Pinned provider rule-update implementation](https://github.com/hashicorp/terraform-provider-aws/blob/v6.58.0/internal/service/events/rule.go)
- [Step Functions events](https://docs.aws.amazon.com/step-functions/latest/dg/eventbridge-integration.html)
- [Execution-history retention](https://docs.aws.amazon.com/step-functions/latest/dg/service-quotas.html)
- [Incomplete multipart lifecycle](https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpu-abort-incomplete-mpu-lifecycle-config.html)
- [User-defined cost allocation tags](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/custom-tags.html)
- [Cost-allocation activation timing](https://repost.aws/knowledge-center/organizations-no-cost-allocation-tags)
- [Lambda event-source mapping tags](https://docs.aws.amazon.com/lambda/latest/dg/tags-esm.html)
- [CloudWatch resource tagging](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Tagging.html)
