# L-57 / L-58: supervised live-window qualification, 2026-09-08

## Disposition and scope

**Passed for supervised short dev windows.** The fixed L-57 observation and
L-58 deadline cleanup passed on clean source
`5aed8a6e67b0e34be1ef94a40b6ad3c470169bbd`. The earlier supervised
[unpark/early-park round trip](../runbooks/live-window.md#successful-supervised-round-trip-2026-09-08-utc)
and failed-build recovery supply the other lifecycle evidence.

The owner accepted this supervised-use boundary on 2026-09-08 in
[ADR-030](../adr/030-bounded-live-windows-and-terraform-parking.md#supervised-use-qualification).
An operator remains available through terminal workflow/build results and
independent parked verification, and handles failed or uncertain cleanup.
Unattended use remains gated by
[L-59](https://github.com/lilabrooks/aws-public-change-feed/issues/205).
Formal L-57/L-58 closure follows publication and the final revision's checks.

This confirms only the existing persistent dev deployment. It preserves the
[M3 assessment's scope and limits](m3-production-readiness-assessment-2026-09-06.md).
It does not qualify continuous operation, a larger load envelope, a new
deployment, guaranteed notification delivery, or a hard billing cutoff.

## Preflight and evidence reuse

Fresh parked central/control plans had no changes. The actual workflow and IAM
matched the reviewed source, and AWS accepted the rendered definition without
warnings or truncated diagnostics. The prepared plan changed monitoring only.

The five deployed functions' exact packages and application configuration,
active release, retained resource identities, and schedule expressions matched
the M3 comparison. Managed lifecycle controls were checked separately.
Application source, corpus, dev policy, and deployment inputs were unchanged.
The existing Slack, fixed-load, recovery, configuration-rollback, and
application-rollback evidence was reused within its original limits. No
synthetic traffic, release, package, or load cohort was introduced for this proof.

## Fixed observation

After independently verified live readiness, the operator declared one
20-minute observation with fixed bounds and expected schedule opportunities.
The observation did not expand when the natural feed sample was quiet.

| Scheduled component | Expected opportunities | Invocations | Heartbeats | Lambda errors / throttles |
| --- | ---: | ---: | ---: | --- |
| Feed watcher | 1 | 1 | 1 | 0 / 0 |
| Outbox dispatcher | 20 | 20 | 20 | 0 / 0 |
| Recovery reconciler | 4 | 4 | 4 | 0 / 0 |

These scheduled-function metrics had datapoints. Worker and shadow had no
recorded activity in the fixed cohort; absent datapoints are not measured zeros.
All four feeds succeeded, and all 28 alarms were `OK` at the cohort's closing readback.
Release-verification and raw-snapshot failures were zero, with no recorded
watcher fault or incomplete-run emissions in the cohort.

One natural delivery posted before the fixed observation,
with one recorded Slack attempt and HTTP 200. It is not a positive result from
the 20-minute cohort. The cohort itself produced no newly posted delivery.
The final durable result was 16 retained `posted` records, no recorded unresolved
delivery work, and zero approximate queue counts. Nothing was purged to obtain
that result. The quiet fixed observation is terminally `passed` for L-57's
scheduled-health check, not a new positive-match or capacity proof.

## Deadline cleanup and parked result

The window used the reviewed 90-minute target, including setup and cleanup.
After readiness, only the verified local Python waiter was terminated with
`SIGKILL`. A human continued supervising. The same AWS owner, source bundle,
deadline, and cleanup boundary remained in place, with no early-stop request.

The callback task timed out into the normal parking path. Activation, parking,
and the workflow succeeded without a cleanup retry or deadline overrun.
Saved transition plans and actual API events showed schedule changes in both
directions and mapping updates without a metrics payload. The fixed rule tags
were preserved.

The verified draining receipt preceded consumer stopping by 319 seconds.
The mapping-disable API event preceded final alarm removal by 327 seconds.
These observed intervals accompany the controller's retained invocation-wait
evidence; API event spacing alone is not a separate execution trace.
Activation build wall time was about 2.5 minutes and parking build wall time
about 12.3 minutes. One sample does not establish worst-case budgets, so the
reviewed reserves and minimum windows remain unchanged.

Independent final readbacks confirmed:

- All three runtime schedules and the worker queue mapping disabled, all five
  functions at zero reserved concurrency, and no provisioned capacity/pollers
  or enabled mapping metrics.
- All 28 deployment metric alarms and the operations dashboard absent.
- Application/package/release and retained resource identities preserved;
  normal feed and delivery state was allowed to advance during live operation.
- Fresh central and control Terraform plans with no changes, and terminal
  workflow/build results.
- Zero approximate queue counts on a later readback beyond the watcher's
  maximum async event age. This still does not prove an empty Lambda async
  backlog; unpark may resume retained accepted events.

Eligible successful closeout was verified. Control objects remain retained,
and no pruning or application-retention/PITR change occurred.

## Notifications and remaining limits

The owner already confirmed a build-failure email and a terminal-failure email
from the earlier failed attempt. The terminal confirmation did not identify
the workflow-event route versus direct exhausted-cleanup publication. Neither
terminal route is marked separately receipt-qualified. Direct publication
success is not inbox-delivery evidence. Failed-publication preservation remains
unproved live; repository checks and definition inspection are separate evidence.

The owner accepted these gaps for supervised use. L-59 first inspects existing
emails and reuses unchanged evidence. It retains route-specific receipt and
live failed-publication qualification as unattended-use gates. No additional
failure injection ran for this closeout. Both terminal routes share SNS and
remain best effort even after qualification.

Parking removes monitoring resources and stops ordinary runtime activity after
in-flight work ends. Logs, S3, DynamoDB storage/PITR, secrets, and control storage
remain possible costs; builds and requests add usage when operated. This proof
did not measure the AWS bill or net savings.

## Evidence custody and next use

The restricted archive contains the exact observation bounds, caller and state
identities, immutable bundle comparison, plans, metric/log results, API events,
readbacks, and closeout. Its 124 retained files passed archive verification.
Raw execution identifiers, private configuration, operational timestamps, and
receipts are not published here. Obtain the restricted bundle from the owner
for an audit or recovery; this public summary is not an apply input.

The [runbook](../runbooks/live-window.md#supervised-use-and-requalification)
defines per-use checks and the changes that require requalification. Future
short tests use fresh reviewed inputs and bundles, a predeclared terminal
condition, and early parking once their evidence is complete. They need not
repeat this 90-minute deadline protocol merely to use the unchanged controller.

References verified: 2026-09-08.
