# Agent tooling: the AWS Knowledge MCP server

Handoff notes for anyone working this repository with Codex or Claude Code. This is the reasoning behind the tool, not only the rule. If the reasoning is wrong, say so — the last section lists what would change it.

## What is configured

The AWS Knowledge MCP server, declared once per host because the two read different files and share no format:

- Claude Code: [`.mcp.json`](../.mcp.json)
- Codex: [`.codex/config.toml`](../.codex/config.toml)

A change to one needs the same change to the other. [`tests/test_agent_config.py`](../tests/test_agent_config.py) fails when they drift.

Endpoint `https://knowledge-mcp.global.api.aws` over HTTP. No AWS account, no credentials, no CLI install, rate-limited. Five tools: `search_documentation`, `read_documentation`, `list_regions`, `get_regional_availability`, `retrieve_skill`.

`search_documentation` does not return one kind of result, and the difference matters for how much weight its output carries:

| Result shape | Where it comes from | How faithful |
| --- | --- | --- |
| Documentation chunk | `reference_documentation`, `general` | Verbatim page text, including headings and note blocks |
| Announcement summary | `current_awareness` | Rewritten in third person. Not the announcement's wording |
| Agent skill | any topic | `skill_name` and `skill_description` only. No page text, no URL |

Measured on 2026-08-04: an S3 conditional-writes chunk matched the live page exactly, while a `current_awareness` result rendered ["We're announcing availability changes"](https://aws.amazon.com/about-aws/whats-new/2026/03/aws-service-availability/) as "AWS has announced changes to various services and features." Same facts, different voice and different characters.

`read_documentation` converts a full page to markdown, which is faithful to the page but is still a page rather than a feed item.

Codex loads project configuration for trusted projects only. If the server does not appear under `/mcp`, trust the project first.

## Why it is here

Two jobs, both narrow.

**AWS semantics for the milestones that touch AWS.** Milestone 2 needs S3 conditional writes, object versions, and compare-and-swap promotion. Milestone 5 needs SQS FIFO deduplication behaviour, DynamoDB conditional expressions, and TTL semantics. Milestone 6 needs IAM least-privilege shapes and the S3 backend lockfile. These are areas where a stale or approximate mental model produces code that looks right and behaves subtly wrong.

**Discovering how AWS words announcements.** The matcher uses literal configured phrases. Its recall depends on vocabulary coverage, and the gaps found so far were vocabulary rather than inflection: `managed runtimes`, `cumulative updates`, `general distribution release`. Searching how AWS historically phrases end-of-support and deprecation notices is a legitimate way to propose candidate terms.

With one limit that follows from the table above: `current_awareness` paraphrases. It is good at locating *which* announcements exist and what they concern, and it cannot be quoted as evidence of how AWS worded one. A term read off a summary is a guess about vocabulary that the summary itself may have introduced. Use the result's URL to reach the announcement, and confirm any candidate phrase against the feed text in step 2 below.

## Where it must not be used

**Never as a source of the `title` and `summary` strings in a corpus entry.**

The rule is about those two fields, not about announcements as a subject. Researching an announcement through the server is fine and often the fastest route; only the scored characters are reserved.

Permitted, and useful:

- Finding candidate announcement URLs to label.
- Investigating AWS terminology and what a change actually does.
- Helping adjudicate an expected label, or writing the note that explains one.
- Finding the documentation page that explains the change behind an announcement.

Forbidden: pasting server output into `corpus/announcements.json` as an item's title or summary.

The reason is not that the text is processed, though `current_awareness` output is. It is that a page body is a *different artifact* from the feed item the runtime normalizes, and matching is literal on exact characters. Corpus text has one legitimate production path:

1. `_rss_items` in [`feedparse.py`](../src/aws_public_change_feed/feedparse.py) takes RSS `<description>`; `_atom_entries` takes Atom `<summary>` or `<content>`.
2. `normalize_item` in [`announcements.py`](../src/aws_public_change_feed/announcements.py) sanitizes and truncates at the production limits.
3. `make screen-feeds` reaches live text through that same `FeedWatcher`, in [`screen_feeds.py`](../scripts/screen_feeds.py).

Text arriving any other way is a string the matcher never sees. This is not hypothetical: corpus text staged by a different code path once made reported precision and recall describe text the runtime never encounters.

What enforces this is weaker than the rule, and worth knowing precisely. `AGENTS.md` requires corpus text to come from the acquisition path. [`test_corpus_evaluation.py`](../tests/test_corpus_evaluation.py) then catches the one failure actually observed — a historical summary carrying the truncation marker but ending well short of the production limit — and separately checks that historical text is a fixed point of the production sanitizer. Neither test can prove a given string travelled through `FeedWatcher`, because a short, clean string satisfies both. Provenance rests on the `AGENTS.md` rule and on review; the tests catch the specific way it broke before.

**Never in the runtime.** MCP is agent-facing; a scheduled Lambda has no MCP client. Beyond that, chapter 04 mandates RSS and Atom acquisition with host allowlisting, address validation, TLS, no redirects, bounded responses, DOCTYPE refusal, conditional requests, snapshots, and checkpoints. ADR-009 and ADR-017 scope the product to approved public feeds, so replacing them with a hosted knowledge API is a new trust boundary and needs a new accepted scope decision.

## Which source answers which question

The server is the default path for AWS documentation. It is not the only path, and configuring it did not retire `WebFetch` or web search.

| Question | Source |
| --- | --- |
| AWS API parameters, service semantics | Knowledge MCP `search_documentation`, then `read_documentation` |
| A documentation page whose URL is already known | Knowledge MCP `read_documentation` |
| Which announcements exist on a topic | Knowledge MCP `current_awareness`, then web search when ranking misses |
| Corpus `title` and `summary` | Runtime acquisition or a retained raw snapshot. Never either tool |
| Moto, GitHub, HashiCorp, RFCs, other vendors | `WebFetch` or web search. Outside the server's allow-list |
| Verifying a link, or confirming a claim independently | `WebFetch`, deliberately not the server |

The last row is the one to keep. Checking the server's output with the server is not a check. The verbatim finding above only surfaced because a `current_awareness` summary was compared against the live page through a different tool.

Both routes earn their place. The server answered S3 conditional writes in one call, with the exact `If-Match` and `If-None-Match` semantics milestone 2 needs. Searching it for the `managed runtimes` announcement returned a general service-availability page rather than the App Runner release note that ordinary web search finds — the same gap in ranking that made the term a corpus miss in the first place.

## The workflow for vocabulary discovery

Proposing a risk term from what the server tells you is the one path that touches configuration. It has a fixed order, and skipping a step is how a false positive reaches Slack.

1. Use the server to locate announcements of the change type, then follow the result URLs to read the announcements themselves. A `current_awareness` summary is a pointer, not a quotation: any phrase taken from it is a hypothesis about wording, and possibly the summarizer's wording rather than AWS's.
2. Screen each candidate term against the live feeds with `make screen-feeds`, which fetches through the runtime acquisition path so the text screened is the text the matcher sees. Screening against anything else is what previously hid a false positive.
3. Judge the term on true positives against false positives in that sample. A term with no true positive and any false positive is removed rather than patched with a `none` exclusion, because an exclusion only chases one phrase.
4. Only then edit `examples/config.yaml`, recalculate the config hash, the derived `release_id`, and the release references in the three dependent fixtures.
5. Rerun `make evaluate-corpus` and record the figures. Corpus recall after a term change is close to circular, since the terms were chosen to close those items; the live screen is the number that carries weight.

## Judgement calls worth challenging

Recorded so a second agent can disagree with the reasoning rather than rediscover it.

**Credentialed AWS access stays out of the default setup.** The Agent Toolkit was declined on the strength of its installer, which takes over the AWS CLI, the shell profile, a browser login, and the rules files in `CLAUDE.md` and `AGENTS.md`. That reasoning was too narrow to carry the general conclusion. Lighter credentialed options exist: the AWS API MCP server installs through `uvx` without touching the shell profile or writing rules files, and it is now superseded in turn by the official AWS MCP server.

So the installer is a reason to decline one product, not a reason to decline credentials. The load-bearing reason is the last judgement call in this section: an authenticated server produces knowledge in a transcript, and this repository accepts checked-in tests and recorded output as evidence. A credentialed server could help provision or diagnose a test environment without ever being the thing that demonstrates correctness. Adding one is a real option; it is just not on the path to any current milestone.

One correction to that thread, since it cost a round of review. The Knowledge server itself carries no deprecation or migration notice — its README states general availability and no authentication as of 2026-08-04. The supersession chain runs through the API server, not this one. Nothing about the current configuration is stale.

**The real milestone-2 decision is not about tooling.** It is `boto3` with `moto` versus `boto3` against a real account for the integration tests covering compare-and-swap promotion, exact object versions, and concurrent publishers. Mock fidelity is what would be trusted. That decision is open.

**A research answer is not evidence here.** `AGENTS.md` requires a regression test for every rejected mutation, and the goal requires measured evidence for production readiness. Anything learned from the server has to become a test before it counts.

## What would change these conclusions

- If a labeling pass produces misses that are mostly inflections of already-configured terms rather than new vocabulary, the literal-phrase design deserves revisiting against a restricted pattern syntax.
- If the server's regional-availability tools turn out to answer a question the specification leaves open, its role widens beyond documentation lookup.
- If `current_awareness` starts returning verbatim announcement text, the result table above is wrong and the vocabulary workflow gets a shorter path. Re-measure before trusting it; the check is one search against one live page.
- If a milestone needs AWS API calls to make progress rather than to verify it, the credentialed decision reopens on that evidence.

The corpus-boundary question in the first draft is settled. Codex reviewed it, the rule held, and its wording was too broad: it read as forbidding announcement research rather than reserving two fields. Narrowed above. The property to preserve in any future revision is unchanged — corpus text equals what the matcher sees in production.

References verified: 2026-08-04.

- [AWS Knowledge MCP server](https://awslabs.github.io/mcp/servers/aws-knowledge-mcp-server)
- [AWS API MCP server](https://awslabs.github.io/mcp/servers/aws-api-mcp-server)
- [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)
