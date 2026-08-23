# AGENTS.md

## Mission

Build and maintain AWS Public Change Alerting as a public-feed intelligence service. The product ingests approved public AWS feeds, produces explainable route-scoped candidates, and uses Slack as its delivery channel.

## Read order

1. `docs/GOAL.md`
2. `docs/architecture/README.md`
3. `docs/architecture/specification/01-overview.md` through `06-acceptance-and-generation.md`
4. Applicable accepted ADRs in `docs/adr/`
5. Schemas, examples, validators, and tests

If documents disagree, accepted ADRs govern decisions, numbered specifications govern required behavior, schemas govern file shape, and semantic validators govern cross-file invariants. Update every affected layer together.

## Scope guardrails

Keep work inside the boundary set by ADR-017:

- Public AWS RSS and Atom acquisition.
- Deterministic service and risk matching.
- Static environment/profile/customer/route mapping.
- Candidate history, durable outbox, retries, dedupe, and Slack delivery.

Do not add customer-account access, account telemetry, cost collection, security-finding ingestion, remediation, ticketing, incident workflows, or generic external adapters without a new accepted scope decision.

Use “potentially relevant” for environment matches. Public announcements do not prove customer impact.

## Change discipline

- Preserve deterministic identity algorithms and null-framed hashing unless a contract version changes.
- Reject unknown fields in owned schemas.
- Add a regression test for every rejected configuration or contract mutation.
- A determinism test is not coverage. `same input, same output` passes while every output is wrong. Any value the canonical bundle commits and the runtime also derives needs a test that recomputes it from the fixture, so the runtime is bound to the contract rather than to itself. `tests/test_identity.py` is the pattern.
- A claim about how a test behaves is not evidence until the test is made to behave that way. Before recording that a test guards a condition, or that it would silently stop guarding one, invert the condition and run it. Reading the assertion is not enough: a rationale asserting that a stale date fixture would silently stop exercising its check reached a commit message, a pull request, and a checked-in comment before one short script showed the assertion fails loudly instead.
- Prefer a fixture that states its condition over one that happens to satisfy it today. A date literal valid only while the fixed clock sits in some window coordinates two constants through a comment; derive it instead, so the fixture reads as `AS_OF + timedelta(days=1)` rather than a date needing a second edit when the clock moves.
- When a function depends on a particular normalization, apply it inside that function rather than trusting callers. A parameter documented as "already normalized" is where two callers disagree and the mismatch fails silently.
- Keep examples valid and recalculate release, candidate, and request hashes after relevant fixture edits.
- Keep runtime credentials out of configuration, inventory, candidates, logs, and fixtures.
- Use immutable configuration releases and exact object versions.
- Treat DynamoDB as the delivery system of record and SQS as transport.
- Preserve the explicit `delivery_unknown` state. Never claim exactly-once Slack delivery.
- Add or update an ADR when a change alters product scope, trust boundaries, identity, delivery guarantees, state ownership, or version policy. Continue the three-digit numbering, mark new decisions `- Status: Proposed` for the owner's review, and list the ADR in `docs/architecture/README.md`.
- When source changes, update the documents that govern it: `schemas/`, `examples/`, and `scripts/validate_config.py` are governed by `docs/architecture/specification/03-configuration.md` and ADR-011; `src/` and `corpus/` are governed by chapter 04 and ADR-009, with corpus rules and thresholds in ADR-018; `tests/` is governed by chapter 06. If the governing document does not need to change, say why in the commit message or pull request.
- A matcher or configuration change reruns `make evaluate-corpus` and records the reported figures. Chapter 04 requires the promotion record; the harness prints it.
- Corpus text comes from the runtime acquisition path, never from an ad-hoc fetch. Text normalized differently from production makes both the labels and any term screen measure something the matcher never sees. A shorter-truncated corpus previously hid a false positive that only appeared in the full text.
- Adding or removing a risk term reruns `make screen-feeds` when network is available, and every reported unlabeled match is either labeled into the corpus or traced to the rule that fired. A term with no true positive and any false positive is removed rather than excluded case by case.
- Keep `docs/architecture/README.md` current when specification chapters or ADRs are added or renamed. It is the single index and the human reading order.

## Work selection

- When the user asks you to choose or continue the next repository backlog
  item without naming one, run `make next-work` first. It audits the active
  GitHub backlog, loads `.github/work-sequence.yaml`, and checks that governed
  milestone assignments match the versioned sequence.
- A `ready` result names the only item the sequence currently permits. A
  `blocked` result is a stop condition; report its owner, trigger, or dependency
  reason without skipping into a later stage. An explicitly named user task
  remains the controlling scope.
- The selector is read-only. Implementation authority and every GitHub, AWS,
  Slack, publication, or merge mutation remain separate.

## Verification

Use Python 3.12 or newer. Before handing off a change, run:

```bash
make check
```

When network access is available, also run:

```bash
make references-online
```

CI enforces this now, so skipping it delays the failure rather than hiding it: `reference-links.yml` runs the same check on any pull request that changes Markdown. It was previously scheduled-only, and a dead link introduced by a pull request merged green and broke the following Monday's sweep on `main`. Running it locally is still the fast path — the gate tells you on push, the local run tells you before it.

Review `git diff --check`, inspect the complete diff, and remove generated caches. Report checks that could not run and why.

## Working across Codex and Claude Code

This file is the shared instruction source. Codex reads it directly; `CLAUDE.md` imports it so Claude Code loads the same rules. Put repository-wide guidance here, never in a host adapter, and never duplicate it between the two.

Three differences between the hosts are deliberate:

- `CLAUDE.md` also imports `docs/GOAL.md` and `docs/architecture/README.md` so Claude Code loads them automatically. Codex has no import mechanism, so it follows the read order above. Both hosts end up with the same material; only the loading differs.
- The AWS MCP server is declared twice, in `.mcp.json` for Claude Code and `.codex/config.toml` for Codex, because the hosts read different files and share no format. A change to one needs the same change to the other. Codex loads project configuration for trusted projects only, so trust the project if the server does not appear.
- `.claude/settings.json` denies reading `.env` files and denies the server's four account-capable tools. Codex has no equivalent project setting, so those controls protect Claude Code sessions only. On Codex the tools are merely unusable while nobody authenticates, which is a weaker guarantee. Treat both as a convenience rather than an enforced boundary on either host, keep secrets out of the repository regardless, and do not authenticate the server.

## Automatic governing-skill audit

Automatic governing-skill audit: enabled
Repository ID: `aws-public-change-feed`

This opt-in authorizes the installed ledger skill to launch at most one
read-only audit subagent per execution epoch under its automatic-audit decision
contract. It grants no provider dispatch, external disclosure, repository or
backlog mutation, AWS, Slack, or GitHub action, publication, or merge authority.

## Repository layout

- `README.md`: human entry point and current status.
- `.mcp.json` and `.codex/config.toml`: the AWS MCP server, configured for Claude Code and Codex respectively. Used unauthenticated: the documentation tools need no credentials, and the account-capable tools the same server exposes fail without them. It is read-only in practice rather than by construction, so never authenticate it to do repository work. It is the default path for AWS documentation and API semantics, for locating announcements, and for researching how AWS words them; `WebFetch` and web search remain in use for non-AWS sources, for ranking gaps, and for confirming the server's output independently. Never use either to supply the `title` or `summary` of a corpus item: those must carry the feed item's text as normalized by the runtime acquisition path, and matching is literal on exact characters. Researching an announcement through the server is fine — only the scored fields are reserved. `docs/agent-tooling.md` carries the reasoning, the per-topic fidelity of the search results, the vocabulary-discovery workflow, and the judgement calls open to challenge.
- `docs/agent-tooling.md`: handoff notes on when the MCP server helps, where it must not be used, and what evidence would change those conclusions.
- `docs/GOAL.md`: product outcome, scope, and implementation milestones.
- `docs/architecture/README.md`: architecture index and document map.
- `docs/architecture/specification/`: normative requirements in reading order.
- `docs/adr/`: accepted architectural decisions.
- `docs/runbooks/`: operational response procedures.
- `schemas/`: machine-readable contracts.
- `config/`: reviewed environment policy inputs, including the canonical dev policy.
- `examples/`: canonical valid fixtures.
- `scripts/`: local validators.
- `tests/`: regression coverage.
- `infra/bootstrap/`: built Terraform remote-state bootstrap root.
- `infra/central/`: built Terraform service root.
- `corpus/`: labeled announcements and the approved matching thresholds.
- `src/`: Python runtime packages.

Create planned directories only when implementation files are ready. Empty placeholders add clutter.
