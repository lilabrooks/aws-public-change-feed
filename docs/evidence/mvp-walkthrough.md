# Service walkthrough: delivery, lifecycle, and readiness

One walkthrough covers the completed M1, M2, and M3 milestones. Slides 2 and 3
explain the runtime architecture, shared state, and operational evidence. The
remaining scenes cover the recorded live results, lifecycle changes, and
application rollback proof.
The opening and narration describe the completed project at the reviewed dev
boundary. Private AWS account and Slack details are omitted.

## Watch or download

- [Watch the narrated video with captions](https://lilabrooks.github.io/aws-public-change-feed/)
- [Download the 1080p MP4](../../site/media/walkthrough-v3/aws-public-change-alerting-walkthrough.mp4)
- [Open the original MVP slides as PDF](../../site/media/mvp-evidence-v2/aws-public-change-alerting-mvp-evidence-v2.pdf)
- [Download the original MVP PowerPoint](../../site/media/mvp-evidence-v2/aws-public-change-alerting-mvp-evidence-v2.pptx)
- [Read the WebVTT captions](../../site/media/walkthrough-v3/captions.vtt)
- [Verify the current media hashes](../../site/media/walkthrough-v3/SHA256SUMS)

The site plays a 720p copy and links the 1080p download. The complete narration
uses the original voice setup: Kokoro 0.9.4, Kokoro-82M, American English,
`af_heart`, speed 1.0. The [media notes](../../site/media/walkthrough-v3/README.md)
record the chapter timings, editable frames, and explicit A-W-S pronunciation
rule. Playback does not invoke the model.

## Evidence behind the walkthrough

The [platform specification](../architecture/specification/02-platform.md)
and [processing requirements](../architecture/specification/04-alert-processing.md)
define the Lambda roles, durable state, and delivery behavior. The
[operations specification](../architecture/specification/05-security-and-operations.md)
owns monitoring and credential boundaries. [ADR-028](../adr/028-separate-cloudtrail-evidence-for-dynamodb-restore-identity.md)
explains CloudTrail's separate role in verifying recovery-request identity;
it does not establish restored table contents.

The [README milestone records](../../README.md#project-status) cover the M1
cohorts and M2 migration. M3 later passed production readiness for the exact
reviewed deployment and envelope. The [readiness explanation](../production-readiness.md)
links the policy decision, reused evidence, [L-54 rollback record](l54-application-rollback-2026-09-07.md),
and [final assessment](m3-production-readiness-assessment-2026-09-06.md).
The video summarizes those records; it does not expand their claims.

The historical MVP release and its nine-slide PDF and PowerPoint remain
unchanged. This current walkthrough has a revised opening, eight technical
scenes, and a complete replacement narration.

## Narration transcript

### 1. Delivery, lifecycle, and readiness

AWS Public Change Alerting matches public AWS announcements to configured
services and risk phrases, then sends the results to Slack. M1 proved
delivery, M2 added retention and replay controls, and M3 established readiness
for the reviewed deployment.

### 2. How the Lambda jobs connect

EventBridge schedules three Lambdas. The watcher fetches approved feeds,
matches service aliases and risk phrases, and records candidates before
advancing checkpoints. The dispatcher sends due delivery records to SQS FIFO.
The queue invokes the Slack worker, which sends the message and records the
outcome. The reconciler recovers overdue work and marks expired sends as
delivery unknown. A fifth Lambda, shadow, evaluates the same feed policy in
memory for deployment checks.

### 3. Why these services have separate jobs

DynamoDB keeps checkpoints and delivery history after queue messages are
consumed. Conditional writes coordinate leases, deduplication, and retries
across Lambdas. SQS provides transport and destination ordering. S3 holds
exact configuration releases and retained feed responses for replay.
CloudWatch collects runtime logs and metrics; alarms notify the operator
through SNS. CloudTrail has a narrower role here: proving which table and
timestamp a recovery request used. It doesn't prove restored contents.
Terraform defines the resources and scoped IAM roles, including access to
Slack credentials.

### 4. What M1 exercised

M1's live cohort produced twelve Slack posts, each with one network attempt,
and twelve posted records in DynamoDB. A separate recovery exercise completed
a pending delivery and moved an expired send lease to delivery unknown without
another Slack call. The fixed load run created fifty records in ten minutes.
The owner also received the alarm email.

### 5. What M2 changed

M2 addressed what happens as history accumulates. The migration added expiry
dates to one-hundred-and-sixty-four old rows, leaving the four active
checkpoints alone. Retained-source replay now binds one saved response and
inspected state into a reviewed plan. It can fill missing records but can't
touch feed checkpoints. No live source replay was applied.

### 6. What the matching score supports

The matcher scored twenty-nine true-positive service and risk pairs, with no
false positives or false negatives, on forty-seven labeled items. That passes
the global thresholds. But six configured pairs have no historical positive,
so that score doesn't establish recall on real AWS wording for those pairs.

### 7. The remaining rollback proof

M3 reused earlier evidence where the changes still supported it. The missing
test was application rollback. With durable execution paused, we switched all
five Lambdas to the qualified predecessor and back to the successor. One
shadow run per version returned the same eight candidate identities from
two-hundred-and-forty items.

### 8. The readiness result and its limits

The stopped-state digests matched, then we restored normal operation. The
fixed observation window passed, all twenty-eight alarms were healthy, and
Terraform showed no changes. The owner accepted readiness for one environment
and Slack destination at three-hundred delivery requests per hour. Live
service on restored tables remains unproved.

References verified: 2026-09-06.
