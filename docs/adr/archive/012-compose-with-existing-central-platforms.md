# ADR-012: Compose with existing central platforms

- Status: Superseded by ADR-017
- Date: 2026-07-12

## Context

Some organizations have no central alerting platform. Others already aggregate AWS Health or Security Hub data but do not monitor public AWS announcements, calculate the required cost trends, map customer environments to notification routes, or provide the delivery guarantees in this specification. A mature platform may already provide all of those capabilities.

Deploying duplicate Health rules, aggregation state, Slack delivery, retries, or dedupe would create duplicate alerts and unclear operational ownership. Treating AWS Organizations or a Security Hub administrator account as proof that every alerting capability exists would hide real gaps. The most distinct capability in this repository is the public-feed lane: it turns broad AWS announcements into service-specific, potentially relevant change intelligence for configured customer stacks.

Public-feed relevance is inferred from configured service profiles. It does not prove that an account or resource is affected. AWS Health remains authoritative for account-specific impact, and an existing security platform remains authoritative for Security Hub, GuardDuty, Inspector, and similar findings.

## Decision

Support 3 adoption profiles:

1. **Standalone platform**
   - Use when no suitable central platform exists.
   - Deploy the repository-owned configuration, inventory, public-feed, Health, cost, aggregation, delivery, dedupe, Slack, alarm, and runbook components.
   - Use the appropriate organization, cross-account, or polling modes already defined by ADR-003.

2. **Augment an existing platform**
   - Inventory the capabilities and operational owner of the existing platform before generating IaC.
   - Deploy only missing producers or shared components.
   - Reuse one existing organization Health feed rather than creating a second feed or per-account forwarding path.
   - Leave existing security-finding collection in its current platform.
   - Add the public-feed watcher when equivalent public-change intelligence is absent.
   - Add cost checks, mapping, grouping, delivery, retries, or dedupe only when the existing platform lacks the required behavior.

3. **Existing platform already meets the contract**
   - If the existing platform also provides equivalent public-feed acquisition, deterministic service-and-risk matching, customer-aware routing, explainability, grouping, retries, and route-scoped dedupe, do not deploy a duplicate runtime.
   - The repository can remain a reference specification, test source, or implementation baseline.
   - If public-feed intelligence is the only gap, deploy only that producer plus a reviewed adapter into the existing platform.

AWS Organizations by itself does not select a profile. A central platform exists for this decision only when a named team and deployed components own the relevant ingestion, mapping, state, delivery, and operational response.

All producers must emit a versioned `AlertCandidate` contract before platform-specific delivery. At minimum it carries source type and ID, event time, service, risk type, priority, candidate account and environment IDs, route context when resolved, explainability, and a stable source identity for dedupe. Public-feed candidates must label environment relevance as inferred.

An integration must have one owner for each capability:

- Source acquisition.
- Normalization and matching.
- Customer and route mapping.
- Aggregation.
- Delivery retries and DLQ response.
- Dedupe and ambiguous-delivery handling.
- Operational alarms and incident response.

After a successful handoff, the receiving platform owns downstream retries and dedupe unless a separate adapter decision states otherwise. Two systems must not post the same candidate independently. External EventBridge, SQS, SNS, or API adapter fields must be added only after the actual receiving interface is selected and recorded; this ADR does not invent a generic endpoint.

## External interface selection gate

The concrete external interface is currently unselected because this repository does not name a receiving platform or its supported intake contract. This leaves standalone implementation unblocked. It blocks external adapter code, adapter configuration fields, and augmented-deployment IaC until the following decision is complete.

For each augmented deployment that hands candidates to another platform:

1. Name the receiving platform, owning team, AWS account and Region where relevant, and downstream runbook.
2. Obtain the receiving platform's supported intake interfaces and operational guarantees.
3. Choose exactly 1 interface for the adapter. Use the receiver's owned EventBridge bus, SQS queue, SNS topic, or documented HTTPS API only when that interface can preserve the required candidate identity and evidence fields.
4. Record the choice in an accepted adapter ADR. Do not add every possible transport behind one generic endpoint setting.
5. Add only the selected interface's configuration fields, schema rules, IAM actions, fixtures, contract tests, alarms, and recovery procedure.

The adapter ADR must record:

- Destination identity, account, Region, ownership, and provisioning boundary.
- Exact payload envelope and supported `AlertCandidate` contract version.
- Authentication, authorization, encryption, and resource-policy requirements.
- Maximum payload size and the policy for a candidate that exceeds it.
- The service response or receiver record that proves handoff acceptance.
- Sender timeout, retry, backoff, and pre-acceptance failure handling.
- Handling for a lost or ambiguous acknowledgement.
- Stable `candidate_id`, optional resolved route ID, attempt ID, ordering needs, and the receiver's idempotency behavior.
- The exact point where retry, dedupe, grouping, delivery, alarms, and incident ownership transfer.
- Contract-version compatibility, rollout, rollback, and end-to-end test evidence.

Use these interface-specific acceptance rules when evaluating a choice:

| Interface | Handoff acceptance boundary | Required check before selection |
| --- | --- | --- |
| EventBridge custom event bus | Every submitted entry returns an event ID and the response reports no failed entry for that candidate. | Verify the destination bus, resource policy, matching rule, target, archive or DLQ policy, and receiver ownership. A successful call to a missing bus is not sufficient evidence of a working path. |
| SQS queue | `SendMessage` succeeds and returns a message ID for the receiver-owned queue. | Verify queue ownership, resource policy, encryption, queue type, ordering needs, redrive policy, and receiver idempotency. |
| SNS topic | `Publish` succeeds and returns a message ID for the receiver-owned topic. | Verify topic ownership, resource policy, encryption, subscriptions, subscriber failure handling, and receiver idempotency. Topic acceptance does not prove subscriber processing. |
| HTTPS API | The receiver's documented response and idempotency contract acknowledge the candidate. | Verify endpoint ownership, authentication, certificate validation, timeouts, status and body semantics, rate limits, replay protection, and ambiguous-response handling. |

Before acceptance, the sending adapter owns retries and observable failures. After acceptance, the receiving platform owns downstream processing. The receiver must use stable `candidate_id` for idempotency when a lost response can cause the sender to repeat a request. Once routing is resolved, route-scoped delivery dedupe uses `candidate_id` plus route ID so one route cannot suppress another route's delivery.

## Equivalent public-feed capability

An existing platform covers the public-feed lane only when it:

- Fetches approved AWS public feeds safely and independently.
- Uses deterministic normalization and stable item identity.
- Requires both a configured service match and an allowed risk-rule match.
- Keeps production service profiles and aliases outside application code.
- Maps profile matches to potentially relevant customer environments without claiming confirmed impact.
- Preserves route boundaries and explainability.
- Uses the ADR-013 announcement identity to prevent duplicate delivery across repeated polls and feed overlap.
- Provides observable retries, failure handling, and an owned operational path.

## Consequences

- The standalone design remains the canonical complete example.
- Teams can adopt the public-feed lane without replacing working Health or security aggregation.
- Every integrated deployment needs a capability and ownership matrix before IaC generation.
- A concrete adapter may require a later ADR, schema fields, fixtures, and contract tests.
- A fully capable existing platform is a valid reason to deploy no runtime from this repository.
- Public announcements provide earlier or broader awareness but never replace account-specific Health or security findings.

## References

References verified: 2026-07-12.

- AWS Health organizational EventBridge feed: https://docs.aws.amazon.com/health/latest/ug/aggregating-health-events.html
- AWS Security Hub cross-Region aggregation: https://docs.aws.amazon.com/securityhub/latest/userguide/security-hub-region-aggregation.html
- AWS Cost Explorer organization access: https://docs.aws.amazon.com/cost-management/latest/userguide/ce-access.html
- AWS What's New feed: https://aws.amazon.com/about-aws/whats-new/recent/feed/
- Amazon EventBridge `PutEvents` failure handling: https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-putevents.html
- Amazon SQS `SendMessage`: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_SendMessage.html
- Amazon SNS `Publish`: https://docs.aws.amazon.com/sns/latest/api/API_Publish.html
