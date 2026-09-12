# Service walkthrough: architecture, delivery, and operations

A 5:09 walkthrough explains public-feed matching, the five Lambda roles,
state ownership, and measured delivery, recovery, and load results. It retains
the retention, replay, corpus, and application-rollback details, then adds
supervised operation and the four offline maintenance improvements.
Private AWS account and Slack details are omitted.

## Watch or download

- [Watch the narrated video with captions](https://lilabrooks.github.io/aws-public-change-feed/)
- [Download the 1080p MP4](../../site/media/walkthrough-v4/aws-public-change-alerting-walkthrough.mp4)
- [Open the original MVP slides as PDF](../../site/media/mvp-evidence-v2/aws-public-change-alerting-mvp-evidence-v2.pdf)
- [Download the original MVP PowerPoint](../../site/media/mvp-evidence-v2/aws-public-change-alerting-mvp-evidence-v2.pptx)
- [Read the WebVTT captions](../../site/media/walkthrough-v4/captions.vtt)
- [Verify the current media hashes](../../site/media/walkthrough-v4/SHA256SUMS)

The site plays a 720p copy and links the 1080p download. The complete narration
uses the original voice setup: Kokoro 0.9.4, Kokoro-82M, American English,
`af_heart`, speed 1.0. The [media notes](../../site/media/walkthrough-v4/README.md)
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
unchanged. This revision retains the technical narration from scenes 2–8,
replaces the opening, and adds two operations and maintenance scenes.

The [September 8 supervised-window record](l57-l58-supervised-live-window-2026-09-08.md)
and accepted [ADR-030](../adr/030-bounded-live-windows-and-terraform-parking.md)
own the M4 claims. Its quiet fixed cohort is scheduled-health evidence, with
no new post and no extension. An operator remains available through terminal
parked verification; unattended notification qualification remains pending.
M5’s closed issues record [tracked adapter guards](https://github.com/lilabrooks/aws-public-change-feed/issues/118),
[CLI help contracts](https://github.com/lilabrooks/aws-public-change-feed/issues/108),
[manifest verification](https://github.com/lilabrooks/aws-public-change-feed/issues/194),
and [feed-claim parity and call counts](https://github.com/lilabrooks/aws-public-change-feed/issues/65).

## Narration transcript

### 1. Architecture, delivery, and operations

AWS Public Change Alerting matches public AWS announcements to configured services and risk phrases, then sends route-scoped candidates to Slack.

Each candidate preserves the matched text, potentially relevant environments, and exact configuration release.

The architecture keeps feed checkpoints, queued work, and Slack outcomes traceable through delivery and recovery.

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

### 4. Delivery, recovery, and load

M1's live cohort produced twelve Slack posts, each with one network attempt,
and twelve posted records in DynamoDB. A separate recovery exercise completed
a pending delivery and moved an expired send lease to delivery unknown without
another Slack call. The fixed load run created fifty records in ten minutes.
The owner also received the alarm email.

### 5. Retention and source replay

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

### 7. Application rollback proof

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

### 9. Supervised windows with durable cleanup

M4 qualified supervised short dev windows.

Step Functions owns the deadline; CodeBuild applies reviewed Terraform plans to activate and park the service.

The fixed twenty-minute observation recorded one watcher, twenty dispatcher, and four reconciler invocations, with matching heartbeats and no scheduled-function errors or throttles.

It produced no new Slack posts and ended without extension.

Deadline cleanup survived local waiter loss, disabling four triggers, fencing five functions, and removing twenty-eight alarms and the dashboard.

An operator remains available through parked verification; unattended notification qualification is still pending.

### 10. Maintenance that preserves the contracts

M5 tightened four maintenance checks without activating AWS.

Tracked agent adapters reject credential-bearing configuration, and CLI help tests tolerate terminal wrapping while still requiring the documented wording.

A stable synthetic package manifest is checked separately from current-source verification.

Feed-claim tests now cover expired-lease URL mismatch and exact DynamoDB update and read counts.

These changes preserve the service contracts and supervised operating boundary.

References verified: 2026-09-12.
