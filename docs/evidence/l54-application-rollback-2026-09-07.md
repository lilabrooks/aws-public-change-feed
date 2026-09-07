# L-54 application rollback and restoration

- Exercise date: 2026-09-06 through 2026-09-07
- Environment: persistent `dev`, `us-east-1`
- Terminal disposition: `passed`
- Restricted evidence manifest SHA-256:
  `301c6faa076578acb47a4b2c08bc29abef0d253cf96a3d59eca4063e8038b434`

This application-only follow-up closed the package-transition gap left by
L-42. It selected one qualified predecessor across all five Lambda functions,
checked that package through the deployed shadow path, restored the exact
successor, repeated the shadow check, compared stopped durable state, and
returned the service to normal operation.

No configuration release was promoted. The active pointer remained on release
`8527e2b4…` and VersionId `HJbvljTXyq.N1mExbfOiNn2Sv3vQnm5s` throughout the
exercise. The retained-source replay check stayed preview-only.

The restricted bundle remains outside Git. This public record omits caller
identities, role and resource ARNs, queue URLs, the notification endpoint,
secret identifiers, Terraform bodies, feed payloads, and candidate contents.

## Bound identities

| Identity | Value |
| --- | --- |
| Repository base commit | `db12c51966a8aa432c3ecfeb3e98377087b84a25` |
| Qualified predecessor SHA-256 | `c88b49c8f070f1cb808ac005cbe28b484c14be7b29f50a34e21ef3a7ca85ccbd` |
| Predecessor S3 VersionId | `QXNwt_NBIqp0pNKVFalwbZ72587h.GCc` |
| Successor SHA-256 | `1ae996cabff5e50d9fe3889c67613f983b58be7c590f2c9f1e9a2ec2cbb65dd1` |
| Successor S3 VersionId | `yFAfr5Y8fdEnOatvqVHzXlFej5cXSSTg` |
| Successor S3 checksum | `GumWyr/15Q2f44icZ2E/mDtYvnxZDyyfHpouwsu2XdE=` |
| Configured-handler contract SHA-256 | `8d934863e4305f466c8e4982215f37b48222fee3cb18635509fae7163156cf29` |
| Candidate-set SHA-256 | `456a0feb14c5caa399a2ab2c172a474c96b36c45029afe10ff269325859396d0` |

The successor archive carries the accepted package manifest, passed the
network-disconnected Linux Python 3.12 x86_64 import gate, and was read back
with the S3-computed checksum above. The unchanged predecessor passed its
fixed L-53 byte, source-revision, archive, handler, and Linux qualification.

The run used an explicitly recorded uncommitted source state based on
`db12c519…`. Package metadata, the in-archive source-tree digest, exact archive
bytes, saved Terraform plans, and the final source record bind what was
exercised. This record does not claim that a later commit was deployed.

## Read-only configuration evidence

ADR-026 permits the application-only follow-up because the L-42 configuration
evidence and restricted manifest verified and the recorded comparison found no
unresolved material difference in the reused exact-version, identity,
integrity, compare-and-swap, compatibility-probe, or pointer-read-back claims.

Before downtime, configuration preview SHA-256
`9b57f0a096310266d3058a638f7020c8e686491d8b029dceb916617345bb987c`
resolved the current and retained pointer and release objects. Retained-source
replay preview SHA-256
`50505b103ee7e39fb8eb4e55cc10630a260249653f439bfe886a4cdee4b522ce`
resolved the historical snapshot, route, and four existing candidate and
delivery records. It identified one missing page marker but applied nothing.

The same references resolved against the predecessor and restored successor.
No missing evidence required a configuration mutation.

## Maintenance sequence

Drain plan SHA-256
`ed12a906d17ab7efc18fa4e770b4c426721166b6c1553a373e9dfad97576a195`
disabled the watcher, dispatcher, and reconciler schedules. The worker stayed
available until queues and actionable delivery states were empty. The plan
also removed seven trigger-dependent alarms and made no package change.

Full-pause plan SHA-256
`64f8c8133c012f65a1e0f6d33621190340b355c9675b8209224ee2c6daced889`
disabled the worker mapping and set reserved concurrency to zero for watcher,
dispatcher, worker, and reconciler. Shadow concurrency remained one. Direct
read-back confirmed the complete pause, then the exercise waited the required
300 seconds before capturing the stopped baseline.

Predecessor plan SHA-256
`38073d81fbf606050a4453168e0ba9019c5f5d4cbc6b2bd615ae91610304d9b7`
made seven updates: five Lambda package selections and two artifact-guard
bindings. It changed no trigger or concurrency control. All five functions
reported the predecessor checksum after apply.

Forward-restoration plan SHA-256
`badd9aec6823d6ef1e85b63c33cd0dcce10f60ca33c96c4da162002539058ebe`
made the matching seven updates back to the successor digest, VersionId,
handler contract, and checksum. It also left every stopped control unchanged.

## Shadow results

Each package received one authorized synchronous `RequestResponse` invocation
with `AWS_MAX_ATTEMPTS=1`. An expired SSO token rejected the first predecessor
CLI attempt before Lambda dispatch; the unchanged request was dispatched once
after login. No dispatched sample was repeated.

| Package | Invocation ID | Feeds | Items | Candidates |
| --- | --- | ---: | ---: | ---: |
| Predecessor | `f41f9a27-0cb0-48c4-834d-bf6e4ce4b391` | 4 | 240 | 8 |
| Restored successor | `3b37201c-cc1a-44ef-8720-44c3bb62aff2` | 4 | 240 | 8 |

Both results returned the candidate-set digest recorded above. The shadow
function used fresh in-memory stores. Current role read-back found release-read
and log actions only; its separate invoker role had only
`lambda:InvokeFunction` for the shadow function.

## Stopped-state comparison

The baseline was captured at `2026-09-06T21:54:26Z`, after drain, confirmed
full pause, and the timeout. The comparison was captured at
`2026-09-07T00:29:19Z`, after forward package and shadow verification and before
resumption.

| State | Baseline | Comparison |
| --- | ---: | ---: |
| Source-state items | 651 | 651 |
| Delivery items | 31 | 31 |
| Raw-snapshot versions | 673 | 673 |
| Raw-snapshot delete markers | 0 | 0 |
| Nonzero queue attributes | 0 | 0 |

The canonical item digests for both DynamoDB tables matched. The complete
raw-snapshot version and delete-marker inventory digests matched. Active
pointer VersionId, ETag, and body hash matched. Queue attributes, exact
successor package identities, schedules, worker mapping, and concurrency also
matched.

These records contain inventory counts and canonical digests, not complete
row-level snapshots. No unexpected application write was detected in the
owned tables or raw-snapshot inventory. Slack workspace history was not
queried. The no-post conclusion rests on the disabled delivery path, empty
queues, zero concurrency for every durable executor, and the shadow
function's accepted in-memory composition and permissions.

## Restoration and scheduled operation

Actual-state resume plan SHA-256
`1efe2c72bce9ea7ef75d008c03d2fd92ccb0588e04edbb121bec87fdffbabe31`
contained seven alarm creations, nine planned updates, and no deletion. It
restored the three schedules, worker mapping, four durable concurrency values,
seven trigger-dependent alarms, and the derived runtime-failure queue policy.
It contained no package or configuration change. Terraform applied seven
creates and eight remote updates because the planned queue-policy refresh
resolved without a remote change.

Immediate read-back found all five functions active on the successor,
concurrency at watcher 1, dispatcher 1, worker 2, reconciler 1, and shadow 1,
and all four triggers enabled. All 28 alarms existed with their intended
actions. The queues and actionable delivery inventory were empty.

The normal-operation criteria were frozen before observation. The window ran
from `2026-09-07T00:35:49Z` through `00:53:00Z` and was not extended to obtain
a positive announcement.

| Runtime | Invocations | Errors | Throttles |
| --- | ---: | ---: | ---: |
| Watcher | 1 | 0 | 0 |
| Dispatcher | 18 | 0 | 0 |
| Reconciler | 3 | 0 | 0 |
| Worker | 0 | 0 | 0 |

Watcher, dispatcher, and reconciler heartbeats were present. Release
verification, raw-snapshot, watcher-fault, and incomplete-run metrics stayed
at zero. The quiet worker result was accepted. At the closing read-back, all
28 alarms were `OK`, all queues and actionable delivery counts were zero, and
the active pointer was unchanged.

Final Terraform plan SHA-256
`497c4b43a917d1762d86ce3227d91b2ccdc539a4c51b4f36f61020cc3f64b02a`
returned detailed exit code zero with no non-no-op resource change.

## Terraform state lineage

The exact versioned backend states were read in memory and reduced to lineage,
serial, VersionId, time, and object metadata. State bodies were not retained in
this evidence set.

| Serial | S3 VersionId | Meaning |
| ---: | --- | --- |
| 62 | `MLJuCibYtpC0_Gx.vjzmphU4G.ZWWEK0` | Fully paused state used to create the predecessor plan |
| 63 | `aIfHClfFJJLYbuK14jVh4oYFJx.Neg0M` | Predecessor state used to create the forward plan |
| 64 | `1BSeeH2QGm0RV2jkJmO0QzMHThZHtDQD` | Forward-restored paused state used to create the resume plan |
| 65 | `QrLUsnBc8kKWEkYsbH32e2VWTZnm7BQl` | Final restored normal state |

Serial 65 is the final state. It is not evidence for an earlier plan input.

## Result and limits

L-54 passed for the exact persistent-dev application identities and operating
controls recorded here. It proves a backward and forward five-function
application transition while durable execution was stopped. It does not turn
every retained package into an eligible rollback target, and it does not
expand the accepted recovery claim to service operation on restored DynamoDB
tables.

Prior Slack destination receipt, the fixed 50-message capacity run, corpus
measurements, and the L-41 recovery exercise were reused only after their
mechanism and input comparisons passed. No load cohort, forced Slack post,
configuration promotion, replay apply, or additional feed sample was added to
this exercise.

After this exercise, final candidate
`91f8db0e9b1f6cd6a1889face54589feaef295c0` passed all required checks in
PR #196. The repository owner then accepted L-43's terminal `passed`
disposition. L-54 remains the application-transition evidence within that M3
result; its scope and limits did not expand when the later gate passed.
