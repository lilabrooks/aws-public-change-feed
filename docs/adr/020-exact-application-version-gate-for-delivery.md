# ADR-020: Exact application-version gate for delivery

- Status: Accepted
- Date: 2026-08-10
- Accepted: 2026-08-10

## Context

Every candidate records the application version that produced it. ADR-014 requires the worker to load the candidate's exact release objects, and chapter 05 requires application rollback to preserve historical replay. The worker originally passed the candidate's recorded application version back into the release loader and then judged the candidate with whichever matcher, routing, and explanation code happened to be running. The value selected no compatible code path and constrained nothing.

That behavior makes retained work depend on current implementation details. A later explanation template or matcher change can turn a candidate that was valid when emitted into `failed_terminal`, even though the candidate records the older application version. The terminal write then prevents application rollback from replaying the record.

## Decision

The delivery worker receives the running application version from its composition root and requires exact equality with the version embedded in actionable queued work. It checks the version after validating the stored delivery request and before reading release objects, reading credentials, claiming a sending lease, or calling Slack.

A mismatch returns the message unprocessed with its delivery record unchanged. The future FIFO batch handler includes the message in `batchItemFailures`, so ordinary redrive and DLQ handling preserve it for an operator. It does not become `failed_terminal`, consume a network attempt, advance destination pacing, read a secret, or make a Slack request.

Record-level safety handling remains independent of candidate semantics. A duplicate posted or terminal record can still be acknowledged, and an expired `sending` lease still becomes `delivery_unknown`; the latter protects against repeating a request whose prior network outcome is uncertain.

Historical replay uses application rollback: deploy the exact recorded application artifact, then redrive the retained message. Deployable application artifacts must therefore remain available for at least the delivery-state retention period. A future application may process an older version only through an explicit compatible reader added for that exact version. Version ranges and implicit fallback to current semantics are refused.

### Revision: the version is the deployable artifact digest

- Status: Accepted
- Date: 2026-08-10
- Accepted: 2026-08-10

This revision replaces the preceding sentence that tied artifact availability
to the delivery-state retention period. Unresolved evidence has no terminal
TTL, so that sentence implied unlimited artifact retention without choosing
it.

`application_version` carries `sha256:` followed by the lowercase SHA-256
digest of the exact deployable Lambda package bytes. The build supplies that
same value to the producer and worker composition roots. A source revision or
package label is insufficient because rebuilding it can produce different
dependency and package bytes.

This worker-core milestone implements and tests the comparison port. Lambda
packaging, digest injection, artifact storage, and the pause-and-drain rollout
procedure remain milestone-five deployment work and must exist before a
worker is deployed. Status documents must describe that split directly.

Artifact packages and unresolved delivery records have separate lifetimes.
An unresolved delivery record can remain after an artifact's supported replay
window. Before deployment, the deployment contract must set that replay
window, retain every referenced package for it, and define the operator outcome
when older evidence no longer has a runnable artifact. Until those terms exist,
the repository cannot claim historical application replay as deployed.

### Revision: package replay window and pause-and-drain rollout

- Status: Accepted
- Date: 2026-08-10
- Accepted: 2026-08-10

This revision resolves the replay-window and rollout terms left open by the
artifact-digest revision above.

Deployable Lambda packages are stored by their `sha256:` digest in the
deployment's artifact store. Keep each package for at least 400 days and keep at
least the newest 10 package versions regardless of age. Package retirement must
prove both conditions before deletion. Until that retirement mechanism exists,
packages accumulate.

An unresolved delivery record may outlive that replay window. If its recorded
package is no longer available, automatic processing leaves the record unchanged
and reports the bounded reason `artifact_unavailable` through the deployment's
metrics and operator tooling. The operator may preserve the record, close it
under a documented manual decision, or restore the package from an approved
archive. Current code cannot reinterpret it under another digest.

Application rollout pauses candidate creation, drains actionable work produced by
the current package, records any remaining versions, deploys producer and worker
composition roots with the same new package digest, then resumes candidate
creation. A rollback deploys the retained package named by the queued work before
an operator redrives it. `delivery_unknown` records remain unchanged and do not
block rollout; their package remains subject to the same replay window.

Revisit the 400-day and 10-package floor when measured artifact size makes it a
material storage cost, routine deployments leave old-version actionable work,
or operators need replay after the supported window.

## Alternatives considered

**Always re-evaluate with current code.** This was the original behavior. It can terminally discard retained work after semantic drift and makes the embedded application version observational only.

**Keep a compatible semantic evaluator for every application version.** This permits current deployments to replay older work, but it requires retaining matcher, routing, explanation, and rendering behavior for every recorded build. No deployed workload yet justifies that maintenance cost. An explicit reader remains available when measured replay demand does.

**Change candidate identity to cover every rendered projection.** This would detect stored-field edits without re-deriving them, but it changes the identity contract and does not by itself specify which application can interpret an older candidate. That larger contract change is outside this decision.

## Consequences

- A candidate cannot be terminally rejected merely because current code derives different prose or evidence.
- Application deployments must account for queued work from the prior version. The deployment procedure pauses candidate creation and drains actionable work, or records the remaining versions for later rollback and DLQ redrive.
- The producer and worker must use the same application-version value for a deployment. The future handler supplies that value explicitly rather than reading it from the candidate.
- A mismatch needs a metric and operator-visible reason when the batch handler and alarms are implemented.
- Retaining deployable application artifacts becomes part of the rollback requirement.

## Verification

- A mismatched queued candidate returns unprocessed with the complete delivery record unchanged.
- The mismatch path performs no release read, credential read, Slack call, network-attempt increment, pacing write, or outcome write.
- A matching version continues through exact release loading and the normal worker state machine.
- An expired sending lease still records `delivery_unknown` regardless of application version.

## Rollback and reconsideration

Rollback of this decision removes the equality gate only after another accepted decision defines how current code safely interprets old candidates. Reconsider when routine deployment leaves a material volume of mismatched work, application-artifact retention cannot meet the delivery replay window, or operators need replay without deploying the recorded version.
