# ADR-021: Audited replay of exact terminal delivery records

- Status: Accepted
- Date: 2026-08-13
- Accepted: 2026-08-13

## Context

A delivery record enters `failed_terminal` after a definite failed outcome or
after the bounded automatic retry budget is exhausted. It carries a terminal
retention expiry and is not scheduled for dispatch. ADR-004 forbids automatic
terminal replay, while the operations runbook calls for an audited operator
action after a safe correction. That action does not yet exist.

Some terminal failures can become viable without changing the stored Slack
request or its exact historical configuration and application artifacts. An
operator can repair a missing or wrong credential, restore Slack authorization
or channel membership, unarchive a channel, or correct endpoint-side access.
The same exact request may then be safe to attempt once more.

Other failures are immutable for that delivery record. Candidate/release
disagreement, renderer limits, unusable stored destination data, and payload
shape defects require different input or a different release. ADR-014 and
ADR-020 require the worker to use the exact release and application digest
embedded in the historical record. Replaying that record cannot silently
reinterpret it under current configuration or code.

The historical network-attempt count is delivery evidence. Resetting it would
erase the reason the item became terminal and reopen the automatic budget.
Terminal expiry is also a concurrency boundary: a replay must not revive an
expired or replacement record while retention is deleting it.

## Decision drivers

- Preserve exact request, candidate, release, application, and route identity.
- Permit recovery only after an operator-verified mutable correction.
- Keep terminal records inert unless one explicit preview-first action applies.
- Preserve historical attempts and reserve one auditable operator-approved
  attempt.
- Retain bounded terminal evidence without credentials, URLs, request bodies,
  message bodies, or raw provider responses.
- Make stale, expired, concurrent, and ambiguous outcomes truthful.
- Keep in-memory and DynamoDB behavior identical.

## Decision

We will add an operator-only, preview-first terminal-record replay action. It
may conditionally move one live `failed_terminal` record to immediately due
`pending_queue`, remove terminal expiry, append bounded replay evidence, and
reserve exactly one new attempt.

The action reuses the existing dispatcher, destination pacing, FIFO transport,
and worker path. It preserves the exact stored request, candidate,
configuration release, application digest, destination snapshot, dispatch
generation, creation time, and `network_attempt_count`. It does not call Slack,
SQS, AWS configuration services, or a secret provider itself.

### Eligibility

The implementation owns one exact allowlist. Prefix matching, substring
matching, HTTP ranges, and a default that treats every terminal response as
recoverable are forbidden.

The following pre-call response classes are eligible:

- `credential_read_error`
- `credential_kind_mismatch`
- `webhook_url_rejected`
- `bot_token_rejected`

The following known post-call Slack-side response classes are eligible:

- `http_403`
- `http_404`
- `http_410`
- `slack_access_denied`
- `slack_account_inactive`
- `slack_app_access_restricted`
- `slack_invalid_auth`
- `slack_missing_scope`
- `slack_no_permission`
- `slack_not_allowed_token_type`
- `slack_not_authed`
- `slack_team_access_not_granted`
- `slack_token_expired`
- `slack_token_revoked`
- `slack_channel_not_found`
- `slack_ekm_access_denied`
- `slack_is_archived`
- `slack_not_in_channel`
- `slack_restricted_action`
- `slack_restricted_action_non_threadable_channel`
- `slack_restricted_action_read_only_channel`
- `slack_restricted_action_thread_locked`
- `slack_restricted_action_thread_only_channel`

A record whose bounded outcome metadata truthfully carries
`attempts_exhausted = true` is also eligible, but only for the response classes
that the worker can exhaust: `http_408`, `http_429`, `http_500`, `http_502`,
`http_503`, `http_504`, `slack_internal_error`, `slack_ratelimited`,
`slack_service_unavailable`, `transport_connect_failed`, and
`transport_tls_failed`. This grants one reserved call; it does not reset the
historical count or automatic retry budget. If the reserved call produces
another retryable outcome while the budget remains exhausted, the worker
returns the record to `failed_terminal`.

Every other terminal record is refused. Refused cases include immutable
candidate/release/application disagreement, unsafe or unusable stored
destination data, renderer or payload defects, `http_400`, known Slack payload
shape errors, missing or malformed bounded outcome metadata, an unknown or
unlisted response class, a response class that cannot truthfully carry
`attempts_exhausted = true`, and an expired record.

Eligibility means only that the stored delivery is potentially recoverable
after an operator-verified mutable correction. It does not prove that the
public announcement affected an environment, or that the correction succeeded.

An operator must complete the existing synthetic destination preflight and
supply a bounded reason and evidence reference. The action records that
assertion; it cannot independently prove the external correction.

### Audit record

Terminal replay uses a dedicated bounded history, separate from unknown replay
and found-post reconciliation. Each entry contains:

- decision time;
- operator;
- reason;
- evidence reference;
- prior and newly reserved attempt identifiers;
- prior bounded response class;
- prior `attempts_exhausted` value;
- prior terminal expiry.

The history limit is 25. Older records decode with an empty history. Entries do
not contain request or message bodies, webhook URLs, credentials, tokens, raw
provider response text, or other unbounded data.

### State transition and conflict guards

Preview performs a strongly consistent read and no write. Apply performs a new
strongly consistent read and a single conditional update. The update proves:

- `status = failed_terminal`;
- exact `state_version`;
- exact `last_attempt_id`;
- exact and still-live `expires_at`;
- no `next_attempt_id`;
- available terminal-replay history capacity;
- outcome metadata still eligible under this ADR.

The atomic mutation appends the history entry; sets `pending_queue`; sets
`next_action_at` to the decision time; installs the newly reserved
`next_attempt_id`; removes `expires_at`, stale `queue_message_id`, and the prior
bounded Slack outcome after copying its admitted evidence; and increments
`state_version`. It preserves all immutable delivery inputs and the historical
attempt count.

An expired or changed record is a refusal/conflict, not a replay target. A
conditional failure before mutation is reported as pre-write. A failure after
the update may have reached DynamoDB and is reported as ambiguous unless a
bounded strongly consistent reread proves the exact new attempt and history
entry. Operator output remains bounded and redacted.

## Options considered

### A. Replay the exact historical request after a compatible correction

Selected. This closes the current runbook gap while preserving the immutable
historical contract. Conservative eligibility and exact conditional guards
bound the risk.

### B. Reissue the candidate under a newer release or application

Deferred to backlog item L-15. This would create a new lineage and identity
decision. It cannot be implemented by mutating or reinterpreting the historical
delivery record.

### C. Keep terminal records inert and require manual database editing

Rejected. It leaves the documented recovery path unavailable and replaces a
bounded, tested transition with unaudited state mutation.

## Consequences

Positive consequences:

- Operators can recover a known non-delivery after a compatible mutable repair.
- Exact request/release/application identity and historical attempt evidence
  remain intact.
- Preview, bounded audit data, exact expiry guards, and one reserved attempt
  make the intervention reviewable.
- Existing dispatch, pacing, FIFO, and worker behavior remains the delivery
  path.

Costs and limits:

- The response-class allowlist and its documentation must be maintained as one
  contract. New classes require a deliberate decision and tests.
- The operator remains responsible for truthful correction evidence and the
  synthetic destination preflight.
- One reserved call can fail and return the record to terminal.
- This action cannot repair immutable payload, candidate, release, application,
  or destination-snapshot defects.
- Dedicated history adds bounded record size until terminal retention expires.

## Verification

- Test every allowed response class and representative refusals.
- Test `attempts_exhausted = true` without resetting the counter or budget.
- Prove in-memory and DynamoDB parity for success and every conflict guard,
  including changed/expired TTL, changed attempt/version/outcome, existing
  reservation, and full history.
- Prove old-record decoding, bounded history serialization, and secret/provider
  text exclusion.
- Prove preview is read-only and apply diagnostics distinguish refusal,
  pre-write, success, and ambiguity in all replay modes.
- Prove dispatcher/worker consumption of the reserved attempt under the exact
  release and application digest, including return to terminal after another
  retryable exhausted-budget result.
- Build the Lambda artifact twice and compare membership and digests.
- Run focused checks, documentation/reference checks, `make check`,
  `git diff --check`, complete diff inspection, and hygiene checks.

## Migration and rollback

The change is additive. Existing records have no terminal-replay history and
decode as an empty list. The existing reserved-attempt fields and worker handoff
remain in use.

Rollback disables new terminal-replay mutations. Records already moved to
`pending_queue` continue through the normal exact-request delivery path; they
must not be rewritten to reconstruct the old terminal state. Readers for the
new bounded history must remain deployed until affected records expire or are
otherwise proved absent. Do not roll back to a decoder that rejects the stored
history field.

## Revisit conditions

Revisit this decision if:

- recovery requires a newer configuration release or application digest;
- operators repeatedly need a response class outside the exact allowlist;
- evidence shows an allowlisted class is not safely exact-request-compatible;
- replay volume or operator error makes one-record manual action insufficient;
- the history cap is approached in normal operation;
- the product needs a new automatic attempt budget rather than one explicit
  reserved call.

Newer-release reissue remains the separately tracked L-15 decision.

## References

References verified: 2026-08-13.

- [Slack `chat.postMessage` method documentation](https://docs.slack.dev/reference/methods/chat.postMessage)
- [Slack incoming webhook documentation](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks)
