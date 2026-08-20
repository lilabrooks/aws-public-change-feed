# Agent tooling: the AWS MCP server

Handoff notes for anyone working this repository with Codex or Claude Code. This is the reasoning behind the tool, not only the rule. If the reasoning is wrong, say so — the last section lists what would change it.

## What is configured

The AWS MCP server, declared once per host because the two read different files and share no format:

- Claude Code: [`.mcp.json`](../.mcp.json)
- Codex: [`.codex/config.toml`](../.codex/config.toml)

A change to one needs the same change to the other. [`tests/test_agent_config.py`](../tests/test_agent_config.py) fails when they drift.

Endpoint `https://aws-mcp.us-east-1.api.aws/mcp` over HTTP, **used unauthenticated**. This replaced the AWS Knowledge server on 2026-08-04; see the migration note below for why and what it cost.

The server exposes nine tools, and they do not all behave the same way without credentials:

| Tool | Unauthenticated |
| --- | --- |
| `search_documentation`, `read_documentation` | Work. These are what the repository uses |
| `list_regions`, `get_regional_availability`, `retrieve_skill` | Work |
| `call_aws`, `run_script`, `get_presigned_url`, `get_tasks` | Fail with an authentication error |

That last row is the part to understand before touching this configuration. The old Knowledge server was credential-free *by construction* — it had no account-facing tools to offer. This server is read-only only because nobody has authenticated it. `aws___call_aws` returns "Authentication failed: Unable to verify your user identity" today, and would run AWS CLI commands the moment someone completed an OAuth flow.

[`.claude/settings.json`](../.claude/settings.json) denies those four tools outright. That is a Claude Code control; Codex has no project-level equivalent, so on Codex the only thing standing between the agent and those tools is the absence of credentials. Do not authenticate this server to do repository work. Nothing here needs an AWS account, and ADR-017 keeps account access out of the product.

### When the tools are missing

A session can show no `aws-mcp` tools at all while the endpoint is serving normally. This has now happened twice, on 2026-08-04 and 2026-08-05, from **two independent causes with different fixes**. Both times the harness listed the server among those requiring authentication, and neither time did it require authentication. Signing in is never the remedy.

Start with `claude mcp list`, which distinguishes them. `claude mcp --help` documents the split: unapproved `.mcp.json` servers print `⏸ Pending approval`, approved ones are health-checked.

Both remedies below are Claude Code's, and Codex has neither command. Its analogue of cause 1 is project trust, because Codex loads project configuration for trusted projects only: trust the project, then check `/mcp`. Nothing resembling cause 2 has been seen there, which is not the same as knowing it cannot happen.

**Cause 1 — project-scope approval.** `.mcp.json` is checked into this repository, so Claude Code asks for a one-time approval before loading it, the protection that stops a cloned repository from silently running an MCP server its user never chose. That prompt only appears in an interactive session.

```text
aws-mcp: https://aws-mcp.us-east-1.api.aws/mcp (HTTP) - ⏸ Pending approval (run `claude` to approve)
```

Run `claude` once in the repository and approve it. The approval is recorded in `.claude/settings.local.json`, which `.gitignore` excludes, so it is per-clone and never travels with the repository. Every fresh clone meets this once.

**Cause 2 — a cached needs-auth verdict.** The session exposes only `mcp__aws-mcp__authenticate` and `mcp__aws-mcp__complete_authentication`, the auth stubs, and none of the real tools. The per-server log shows the decision made within milliseconds of startup with no network request:

```text
MCP server "aws-mcp": Skipping connection (cached needs-auth)
```

Logs live under the platform cache directory, which on macOS is `~/Library/Caches/claude-cli-nodejs/<sanitized-project>/mcp-logs-<server>/`. Only the macOS path has been checked here. Clear the classification:

```bash
claude mcp logout aws-mcp
```

Verified 2026-08-05: afterwards a fresh session logged `Successfully connected (transport: http) in 384ms` with `No token data found` throughout, and the five documentation tools appeared. Checked end to end rather than inferred — a headless session called `aws___list_regions` and received 37 regions, while `aws___call_aws` stayed absent because the deny list removes it before the model sees it.

The trap in cause 2 is that `claude mcp list` reports `✔ Connected`, because `mcp list` connects even while sessions skip. A green `mcp list` beside a missing tool surface is therefore the *signature* of this bug, not evidence that nothing is wrong.

**Neither fix is a sign-in, and the wording invites confusing them.** Approval says this repository may load the server. `logout` clears a cached classification and carries no credentials — it is the opposite of signing in, not a sign-out from anything anyone signed into. `call_aws`, `run_script`, `get_presigned_url`, and `get_tasks` still fail without credentials and are still denied by name in `.claude/settings.json`. An earlier revision of this note treated an authentication prompt as a reason to work without the server. That was wrong, and it would have given up a working documentation tool to avoid a risk neither remedy carries.

**Until the tools are back, use `WebFetch` and web search — including for AWS documentation.** This is the ordinary state of a fresh clone, not an edge case: the cause 1 approval only prompts interactively, and a non-interactive session is exactly where cause 1 appears, so the fix can be unavailable at the moment it is needed. Nothing in this repository requires the server. `make check`, the validators, and the corpus harness all run without it, and the questions the routing table sends to `search_documentation` are answerable from `docs.aws.amazon.com` directly, more slowly and with worse ranking.

Falling back changes nothing about corpus provenance. That rule already forbids *either* tool from supplying a historical item's `title` or `summary`, so the reserved fields stay reserved whichever way the documentation was read.

**What set the cached flag was not observed.** The earliest log already said `cached`. Metadata discovery is the likeliest trigger, since the endpoint does advertise OAuth protected-resource metadata, and it matches the contingency the last section of this document already anticipated. Do not state it as the mechanism without watching the flag get set.

**What the endpoint actually asks for.** It publishes OAuth protected-resource metadata, so a client that looks will find it, and the challenge is scoped:

| Request | Response |
| --- | --- |
| `initialize`, `tools/list`, `search_documentation` | 200, no `WWW-Authenticate` |
| `call_aws` | 401 with `WWW-Authenticate: Bearer realm="…/mcp", resource_metadata="…/.well-known/oauth-protected-resource"` |
| `GET /.well-known/oauth-protected-resource` | 200, naming issuer `us-east-1.oauth.signin.aws` |

The challenge appears on the account tools and nowhere else. AWS's own boundary therefore falls exactly where the deny list falls. That is a useful confirmation, not a substitute for the deny list, and on Codex nothing enforces the boundary at all.

**A 401 from this server does not prove a tool exists and needs credentials.** Any unknown `aws___*` name answers with the same authentication error, measured 2026-08-05 in one session:

| Call | Response |
| --- | --- |
| `aws___call_aws` | 401 `Authentication failed: Unable to verify your user identity` |
| `aws___definitely_not_a_tool` | 401, byte-identical message |
| `aws___search_documentation` | 200 with results |

So the scoped-boundary reading above holds only because `tools/list` independently confirms `call_aws` is real. Check a tool name against `tools/list` before reading its 401 as an auth requirement: a misspelling is the cheapest way to talk yourself into signing in, and this document previously contained one. Names also need the `aws___` prefix — an unprefixed `search_documentation` is resolved in a different namespace and returns `-32602 Unknown tool: knowledge___search_documentation`, which is at least honest about being unknown.

The issuer is written without a scheme on purpose. The metadata gives it as an `https://` URL with a trailing slash, but an OAuth issuer identifier names an authorization server rather than a page: that root returns 404, as does its `openid-configuration`. Written in full it would fail the reference check and imply something fetchable.

CloudFront fronts this endpoint, so send `Accept: application/json` when re-checking the well-known document. A cached response once made the table above look wrong and produced a confident correction that was itself the error.

Both causes above are local to the host and neither explains the endpoint changing underneath the repository, which is the third possibility and the only serious one. Probe before concluding that:

```bash
curl -sS -D /tmp/mcp.hdr -X POST https://aws-mcp.us-east-1.api.aws/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1.0"}}}'
```

Then reuse the `mcp-session-id` header it returns for `tools/list`, and for a `tools/call` that exercises a documentation tool:

```bash
SID=$(grep -i '^mcp-session-id' /tmp/mcp.hdr | tr -d '\r' | awk '{print $2}')
call() {
  curl -sS -X POST https://aws-mcp.us-east-1.api.aws/mcp \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' -H "mcp-session-id: $SID" -d "$1"
}
call '{"jsonrpc":"2.0","method":"notifications/initialized"}'
call '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
call '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"aws___search_documentation","arguments":{"search_phrase":"S3 conditional writes"}}}'
```

A 200 with the tools listed means the credential-free property is intact and the gap is on this side. Work back through `claude mcp list` and the per-server log before concluding anything about AWS. An auth challenge on `initialize`, or a `search_documentation` that fails where `call_aws` used to fail alone, is the case the last section describes, and neither approval nor `logout` touches it.

`search_documentation` does not return one kind of result, and the difference matters for how much weight its output carries:

| Result shape | Where it comes from | How faithful |
| --- | --- | --- |
| Documentation chunk | `reference_documentation`, `general` | Verbatim page text, including headings and note blocks |
| Announcement summary | `current_awareness` | Rewritten in third person. Not the announcement's wording |
| Agent skill | any topic | `skill_name` and `skill_description` only. No page text, no URL |

The middle column names indexes inside `search_documentation`, not tools. There is no `current_awareness` tool, and asking for one returns an authentication error rather than an unknown-tool error — see the 401 note above.

Measured on 2026-08-04: an S3 conditional-writes chunk matched the live page exactly, while a `current_awareness` result rendered ["We're announcing availability changes"](https://aws.amazon.com/about-aws/whats-new/2026/03/aws-service-availability/) as "AWS has announced changes to various services and features." Same facts, different voice and different characters.

Re-measured against the new endpoint after the migration, because a table like this is worth nothing if it describes a server the repository no longer talks to. The same query returned a byte-identical paraphrase, so both servers front the same index and this table carries over unchanged.

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

The reason is not that the text is processed, though `current_awareness` output is. It is that a page body is a *different artifact* from the feed item the runtime normalizes, and matching is literal on exact characters.

The rule against pasting server output into those two fields is universal. The provenance requirement behind it is not, because ADR-018 deliberately permits `synthetic` items whose title and summary are authored fixtures — they exercise punctuation variants, Unicode, and hard negatives where no real announcement has the needed shape. So the constraint splits:

- `historical` items claim to be real published announcements. Their text must come through acquisition.
- `synthetic` items are authored, and honest about it. They may not borrow AWS text through a side channel and present it as observed.

Historical corpus text has one legitimate production path:

1. `_rss_items` in [`feedparse.py`](../src/aws_public_change_feed/feedparse.py) takes RSS `<description>`; `_atom_entries` takes Atom `<summary>` or `<content>`.
2. `normalize_item` in [`announcements.py`](../src/aws_public_change_feed/announcements.py) sanitizes and truncates at the production limits.
3. `make screen-feeds` loads feeds, services, and risk rules from the reviewed
   [`config/dev.yaml`](../config/dev.yaml) policy, then reaches live text
   through that same `FeedWatcher`, in
   [`screen_feeds.py`](../scripts/screen_feeds.py).

Text arriving any other way is a string the matcher never sees. This is not hypothetical: corpus text staged by a different code path once made reported precision and recall describe text the runtime never encounters.

What enforces this is weaker than the rule, and worth knowing precisely. `AGENTS.md` requires corpus text to come from the acquisition path. [`test_corpus_evaluation.py`](../tests/test_corpus_evaluation.py) then catches the one failure actually observed — a historical summary carrying the truncation marker but ending well short of the production limit — and separately checks that historical text is a fixed point of the production sanitizer. Neither test can prove a given string travelled through `FeedWatcher`, because a short, clean string satisfies both. Provenance rests on the `AGENTS.md` rule and on review; the tests catch the specific way it broke before.

**Never in the runtime.** MCP is agent-facing; a scheduled Lambda has no MCP client. Beyond that, chapter 04 mandates RSS and Atom acquisition with host allowlisting, address validation, TLS, no redirects, bounded responses, DOCTYPE refusal, conditional requests, snapshots, and checkpoints. ADR-009 and ADR-017 scope the product to approved public feeds, so replacing them with a hosted knowledge API is a new trust boundary and needs a new accepted scope decision.

## Which source answers which question

The server is the default path for AWS documentation. It is not the only path, and configuring it did not retire `WebFetch` or web search.

| Question | Source |
| --- | --- |
| AWS API parameters, service semantics | AWS MCP `search_documentation`, then `read_documentation` |
| A documentation page whose URL is already known | AWS MCP `read_documentation` |
| Which announcements exist on a topic | AWS MCP `search_documentation`, whose announcement hits come from its `current_awareness` index, then web search when ranking misses |
| Region names, or whether a service is available in a Region | AWS MCP `list_regions`, `get_regional_availability` |
| Any of the above while the MCP tools are absent | `WebFetch` or web search, AWS documentation included. See [When the tools are missing](#when-the-tools-are-missing) |
| Historical corpus `title` and `summary` | Runtime acquisition or a retained raw snapshot. Never either tool |
| Synthetic corpus `title` and `summary` | Authored by hand for the category under ADR-018. Never either tool |
| Moto, GitHub, HashiCorp, RFCs, other vendors | `WebFetch` or web search. Outside the server's allow-list |
| Verifying a link, or confirming a claim independently | `WebFetch`, deliberately not the server |

The last row is the one to keep. Checking the server's output with the server is not a check. The verbatim finding above only surfaced because a `current_awareness` summary was compared against the live page through a different tool.

Both routes earn their place. The server answered S3 conditional writes in one call, with the exact `If-Match` and `If-None-Match` semantics milestone 2 needs. Searching it for the `managed runtimes` announcement returned a general service-availability page, while ordinary web search returned the [App Runner runtime end-of-support release note](https://docs.aws.amazon.com/apprunner/latest/relnotes/release-2025-08-28-runtime-eos-update.html) as its first result — the same gap in ranking that made the term a corpus miss in the first place.

## The workflow for vocabulary discovery

Proposing a risk term from what the server tells you is the one path that touches configuration. It has a fixed order, and skipping a step is how a false positive reaches Slack.

1. Use the server to locate announcements of the change type, then follow the result URLs to read the announcements themselves. A `current_awareness` summary is a pointer, not a quotation: any phrase taken from it is a hypothesis about wording, and possibly the summarizer's wording rather than AWS's.
2. Screen each candidate term against the live feeds with `make screen-feeds`, which fetches through the runtime acquisition path so the text screened is the text the matcher sees. Screening against anything else is what previously hid a false positive.
3. Judge the term on true positives against false positives in that sample. A term with no true positive and any false positive is removed rather than patched with a `none` exclusion, because an exclusion only chases one phrase.
4. Only then edit `config/dev.yaml`. The files under `examples/` are an
   independent contract fixture and change only when that fixture itself must
   demonstrate a revised contract; any such fixture edit also recalculates its
   config hash, derived `release_id`, and dependent release references.
5. Rerun `make evaluate-corpus`, which explicitly selects `config/dev.yaml`,
   and record the figures. Corpus recall after a term change is close to
   circular, since the terms were chosen to close those items.

The two measurements answer different questions and neither replaces the other. The production-normalized live screen is the independent check on immediate false-positive risk in the observed feed sample; it reports current matches and an unlabeled count, not a quality rate. The corpus report remains the controlled precision and recall measure. A quiet screen is evidence about the base rate in that window, not proof a term is safe.

## Judgement calls worth challenging

Recorded so a second agent can disagree with the reasoning rather than rediscover it.

**Credentialed AWS access stays out of the setup, and that decision got weaker.** It used to be enforced by product choice: the Knowledge server had no account-facing tools, so declining credentials meant declining to install something. It is now a standing instruction not to authenticate a server already configured, whose account tools are one OAuth flow from working. An instruction is a weaker control than an absence.

The reasoning that survives is the last judgement call in this section. An authenticated server produces knowledge in a transcript, and this repository accepts checked-in tests and recorded output as evidence. Credentials could help provision or diagnose a milestone-2 test environment without ever being the thing that demonstrates correctness — so if that day comes, authenticate deliberately, record why, and expect the deny list to be revisited rather than quietly dropped.

The earlier version of this note declined the Agent Toolkit on the strength of its installer, which takes over the AWS CLI, the shell profile, a browser login, and the rules files in `CLAUDE.md` and `AGENTS.md`. That was always too narrow to carry a general conclusion about credentials, and it is now moot: this repository runs the successor to that toolkit's server, unauthenticated.

**The migration off the Knowledge server, and what it cost.** AWS's [setup guide](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/getting-started-aws-mcp-server.html) tells users of the Knowledge server to switch to the AWS MCP Server and names `aws-knowledge-mcp-server` among the entries to remove. The Knowledge server was not deprecated — it remained generally available with no notice on its [project page](https://awslabs.github.io/mcp/servers/aws-knowledge-mcp-server) — so this was following a vendor recommendation, not evacuating a dying endpoint.

The blocking question was whether the new server could be used without credentials, because "credential-free" was the property the old configuration rested on. Its setup guide documents only OAuth and SigV4, which reads like a hard requirement. The [GA announcement](https://aws.amazon.com/blogs/aws/the-aws-mcp-server-is-now-generally-available/) says documentation retrieval no longer requires authentication, which reads like the opposite.

Settled by probing the endpoint rather than by choosing which page to believe. Unauthenticated `initialize` returns 200 with no auth challenge, `tools/list` returns all nine tools, `search_documentation` returns documentation, and `call_aws` fails with an authentication error. So the migration is viable credential-free. The commands are recorded under [When the tools are missing](#when-the-tools-are-missing), and re-running them on 2026-08-04 returned the same four results.

What it cost is honest to state: the tool surface went from five read-only tools to nine, four of which act on an AWS account. Denied on Claude Code, merely unusable on Codex. The old server could not have been made dangerous by a credential; this one can. That is a real reduction in the guarantee, accepted because the alternative was running a server against its vendor's own instruction.

**The real milestone-2 decision is not about tooling.** It is `boto3` with `moto` versus `boto3` against a real account for the integration tests covering compare-and-swap promotion, exact object versions, and concurrent publishers. Mock fidelity is what would be trusted. That decision is open.

**A research answer is not evidence here.** `AGENTS.md` requires a regression test for every rejected mutation, and the goal requires measured evidence for production readiness. Anything learned from the server has to become a test before it counts.

## What would change these conclusions

- If a labeling pass produces misses that are mostly inflections of already-configured terms rather than new vocabulary, the literal-phrase design deserves revisiting against a restricted pattern syntax.
- If the server's regional-availability tools turn out to answer a question the specification leaves open, its role widens beyond documentation lookup.
- If `current_awareness` starts returning verbatim announcement text, the result table above is wrong and the vocabulary workflow gets a shorter path. Re-measure before trusting it; the check is one search against one live page.
- If a milestone needs AWS API calls to make progress rather than to verify it, the credentialed decision reopens on that evidence.
- If AWS stops serving the documentation tools unauthenticated, the credential-free property is gone and the configuration needs rethinking rather than a quiet OAuth flow. The check is the probe in [When the tools are missing](#when-the-tools-are-missing). Rule out an unapproved project server first: both present as an absent tool surface, and only one of them is about AWS.
- If a host ever gates this server on the advertised OAuth metadata rather than on a 401, the configuration stops repaying its cost there, and the choice is to drop it on that host or to accept documentation lookup through `WebFetch` and web search. Neither is authenticating it. The cached needs-auth verdict of 2026-08-05 is the closest thing to evidence so far, and it is not proof: nobody watched the flag get set, and `claude mcp logout` cleared it without the endpoint changing. Watching one get set would settle it.
- If `claude mcp logout` stops clearing the cached verdict, or the flag returns on every session rather than occasionally, the workaround has become a standing cost and the previous bullet's choice arrives for real.
- If Codex gains a project-level tool deny list, the asymmetry noted above closes and both hosts can enforce the same boundary.

The corpus-boundary question in the first draft is settled. Codex reviewed it, the rule held, and its wording was too broad twice over: it read as forbidding announcement research rather than reserving two fields, and it claimed a single acquisition path for a corpus that ADR-018 lets carry authored fixtures. Narrowed above. The property to preserve in any future revision is unchanged — historical corpus text equals what the matcher sees in production.

References verified: 2026-08-04.

- [AWS MCP server setup, including the switch recommendation](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/getting-started-aws-mcp-server.html)
- [AWS MCP server general availability](https://aws.amazon.com/blogs/aws/the-aws-mcp-server-is-now-generally-available/)
- [OAuth authentication for the AWS MCP server](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/oauth-authentication.html)
- [AWS Knowledge MCP server](https://awslabs.github.io/mcp/servers/aws-knowledge-mcp-server)
- [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)
