# Agent tooling: the AWS Knowledge MCP server

Handoff notes for anyone working this repository with Codex or Claude Code. This is the reasoning behind the tool, not only the rule. If the reasoning is wrong, say so — the last section lists what would change it.

## What is configured

The AWS Knowledge MCP server, declared once per host because the two read different files and share no format:

- Claude Code: [`.mcp.json`](../.mcp.json)
- Codex: [`.codex/config.toml`](../.codex/config.toml)

A change to one needs the same change to the other. [`tests/test_agent_config.py`](../tests/test_agent_config.py) fails when they drift.

Endpoint `https://knowledge-mcp.global.api.aws` over HTTP. No AWS account, no credentials, no CLI install, rate-limited. Five tools: `search_documentation`, `read_documentation`, `list_regions`, `get_regional_availability`, `retrieve_skill`. `search_documentation` returns verbatim page chunks; `read_documentation` converts a full page to markdown.

Codex loads project configuration for trusted projects only. If the server does not appear under `/mcp`, trust the project first.

## Why it is here

Two jobs, both narrow.

**AWS semantics for the milestones that touch AWS.** Milestone 2 needs S3 conditional writes, object versions, and compare-and-swap promotion. Milestone 5 needs SQS FIFO deduplication behaviour, DynamoDB conditional expressions, and TTL semantics. Milestone 6 needs IAM least-privilege shapes and the S3 backend lockfile. These are areas where a stale or approximate mental model produces code that looks right and behaves subtly wrong.

**Discovering how AWS words announcements.** The matcher uses literal configured phrases. Its recall depends on vocabulary coverage, and the gaps found so far were vocabulary rather than inflection: `managed runtimes`, `cumulative updates`, `general distribution release`. Searching how AWS historically phrases end-of-support and deprecation notices is a legitimate way to propose candidate terms.

## Where it must not be used

**Never as a source of corpus text or announcement content.**

Not because the text is processed — `search_documentation` returns verbatim chunks. The reason is that a web-page body is a *different artifact* from the feed item's `<description>` that the runtime normalizes. Matching is literal on exact characters, so scoring against page text measures a string the matcher never sees.

This is not hypothetical. Corpus text staged by a different code path once made the reported precision and recall describe text the runtime never encounters. `AGENTS.md` now requires corpus text to come from the acquisition path, and a test rejects historical text truncated anywhere but the production limit.

**Never in the runtime.** MCP is agent-facing; a scheduled Lambda has no MCP client. Beyond that, chapter 04 mandates RSS and Atom acquisition with host allowlisting, address validation, TLS, no redirects, bounded responses, DOCTYPE refusal, conditional requests, snapshots, and checkpoints. ADR-009 and ADR-017 scope the product to approved public feeds, so replacing them with a hosted knowledge API is a new trust boundary and needs a new accepted scope decision.

## The workflow for vocabulary discovery

Proposing a risk term from what the server tells you is the one path that touches configuration. It has a fixed order, and skipping a step is how a false positive reaches Slack.

1. Use the server to find how AWS phrases the change type. Treat the output as a hypothesis about wording, not as evidence.
2. Screen each candidate term against the live feeds with `make screen-feeds`, which fetches through the runtime acquisition path so the text screened is the text the matcher sees. Screening against anything else is what previously hid a false positive.
3. Judge the term on true positives against false positives in that sample. A term with no true positive and any false positive is removed rather than patched with a `none` exclusion, because an exclusion only chases one phrase.
4. Only then edit `examples/config.yaml`, recalculate the config hash, the derived `release_id`, and the release references in the three dependent fixtures.
5. Rerun `make evaluate-corpus` and record the figures. Corpus recall after a term change is close to circular, since the terms were chosen to close those items; the live screen is the number that carries weight.

## Judgement calls worth challenging

Recorded so a second agent can disagree with the reasoning rather than rediscover it.

**The credentialed Agent Toolkit MCP server was declined**, separately from this one. It installs the AWS CLI, modifies the shell profile, runs a browser login, and writes rules files into `CLAUDE.md` and `AGENTS.md`. Its documentation value is largely pre-empted because specification chapters 02, 04, and 05 already encode the semantics the work needs, and its API-access value produces knowledge in a transcript rather than the checked-in repeatable evidence this project requires.

**The real milestone-2 decision is not about tooling.** It is `boto3` with `moto` versus `boto3` against a real account for the integration tests covering compare-and-swap promotion, exact object versions, and concurrent publishers. Mock fidelity is what would be trusted. That decision is open.

**A research answer is not evidence here.** `AGENTS.md` requires a regression test for every rejected mutation, and the goal requires measured evidence for production readiness. Anything learned from the server has to become a test before it counts.

## What would change these conclusions

- If a labeling pass produces misses that are mostly inflections of already-configured terms rather than new vocabulary, the literal-phrase design deserves revisiting against a restricted pattern syntax.
- If the server's regional-availability tools turn out to answer a question the specification leaves open, its role widens beyond documentation lookup.
- If Codex finds that the corpus boundary above blocks something genuinely useful, the boundary is worth re-examining — but the replacement has to preserve the property that corpus text equals what the matcher sees in production.

References verified: 2026-08-04.

- [AWS Knowledge MCP server](https://awslabs.github.io/mcp/servers/aws-knowledge-mcp-server)
- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)
