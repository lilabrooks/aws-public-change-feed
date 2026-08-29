# AWS Public Change Alerting

[![Repository quality](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/quality.yml/badge.svg?branch=main)](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/quality.yml)
[![Repository security](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/security.yml)
[![Public site](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/pages.yml/badge.svg?branch=main)](https://lilabrooks.github.io/aws-public-change-feed/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

AWS Public Change Alerting reads approved AWS RSS and Atom feeds. It matches announcement titles and summaries against configured service aliases and risk phrases, maps each match to static environment profiles, and sends a route-specific review candidate to Slack.

Each candidate includes the matched text, service, risk type, mapped environments, Slack destination, announcement revision, and configuration release. Public feed matches are **potentially relevant**. Operators confirm account or resource impact with their existing AWS tools.

[![Processing overview: approved AWS feeds pass through the watcher, matching and routing, DynamoDB, SQS FIFO, and Slack delivery; recovery writes back to DynamoDB.](docs/assets/readme-overview.svg)](https://lilabrooks.github.io/aws-public-change-feed/)

[Open the full system diagram and generated Slack message.](https://lilabrooks.github.io/aws-public-change-feed/)

## Project status

GitHub Issues and milestones hold the current backlog state.

| Milestone | State | Result or remaining work |
| --- | --- | --- |
| [D0: first live Slack delivery](https://github.com/lilabrooks/aws-public-change-feed/milestone/1) | Closed | One controlled public-feed candidate reached Slack and DynamoDB recorded `posted`. |
| [M1: dev MVP](https://github.com/lilabrooks/aws-public-change-feed/milestone/2) | Closed | Persistent dev operation, recovery, the declared load case, and alarm receipt were exercised. |
| [M2: lifecycle and replay](https://github.com/lilabrooks/aws-public-change-feed/milestone/3) | Open | Source-state retirement, release retirement, retained-source replay, and watcher and dispatcher correctness fixes. |
| [M3: production-readiness proof](https://github.com/lilabrooks/aws-public-change-feed/milestone/4) | Open | Production policy, recovery objectives, rollback proof, the production gate, and final status reconciliation. |

M1 closed after the 4 runtime triggers were enabled in dev, a 12-message public-feed cohort reached Slack, 2 isolated recovery cases passed, a fixed 50-message run completed at 5 messages per minute, and the owner confirmed receipt of the alarm email. Production readiness remains open under M2 and M3.

See the [open issues](https://github.com/lilabrooks/aws-public-change-feed/issues) for the current work queue. The [goal](docs/GOAL.md) defines product scope and completion criteria; its long-form status will be reconciled after the production gate.

## MVP walkthrough

[![Opening frame from the AWS Public Change Alerting dev MVP evidence walkthrough.](site/media/mvp-evidence-v2/aws-public-change-alerting-mvp-evidence-v2-poster.png)](https://lilabrooks.github.io/aws-public-change-feed/)

The 3:05 public walkthrough traces the Terraform roots, Lambda runtimes, matcher corpus, candidate contract, durable DynamoDB and SQS path, and recorded Slack result. Private account and channel details are omitted.

[Watch with captions](https://lilabrooks.github.io/aws-public-change-feed/) · [Open the slide deck as PDF](site/media/mvp-evidence-v2/aws-public-change-alerting-mvp-evidence-v2.pdf) · [Download the editable PowerPoint](site/media/mvp-evidence-v2/aws-public-change-alerting-mvp-evidence-v2.pptx) · [Read the transcript and evidence boundary](docs/evidence/mvp-walkthrough.md)

## Processing path

1. The watcher fetches configured public hosts with TLS, DNS and IP checks, redirect refusal, byte limits, and parser limits.
2. Feed items are normalized and duplicate URLs are merged across sources.
3. Service aliases and risk phrases are matched against each title and summary.
4. Static profiles map matches to environments and Slack routes.
5. DynamoDB stores the candidate and delivery request before the feed checkpoint advances.
6. The dispatcher sends due work through SQS FIFO. The worker posts to Slack and writes the observed outcome back to DynamoDB.
7. The reconciler retries eligible work and converts expired sending leases to `delivery_unknown`.

## Required runtime rules

| Rule | Effect |
| --- | --- |
| Public inputs only | The runtime has no customer-account credentials, resource discovery, telemetry, or remediation access. |
| Stable IDs | The same announcement revision, service, risk, route, audience, and release produce the same candidate ID. |
| Immutable releases | Every candidate points to exact configuration, inventory, and application versions. |
| DynamoDB owns delivery state | SQS FIFO carries ready work; it does not replace the durable outbox or outcome history. |
| Slack uncertainty stays visible | A timeout becomes `delivery_unknown`. An operator checks Slack before closure or one audited retry. |
| Credentials stay with the worker | Feed content, configuration, candidates, fixtures, and logs contain no Slack secret values. |

The [numbered specification and 20 accepted ADRs](docs/architecture/README.md) define these rules in full.

## Run locally

Python 3.12 or newer is required.

```bash
python3.12 -m venv .venv
. .venv/bin/activate
make install
make check
```

`make check` runs formatting, Python and YAML lint, type checks, schema and cross-file validation, corpus scoring, the full unit-test suite, and whitespace checks. It also validates the Terraform roots when Terraform is installed. CI tests Terraform 1.15.8 and the minimum supported 1.10.0 release.

Useful focused commands:

| Command | Result |
| --- | --- |
| `make validate` | Validates contracts, local references, the public page, and corpus thresholds. |
| `make evaluate-corpus` | Scores matching with the reviewed [`config/dev.yaml`](config/dev.yaml) policy. |
| `make screen-feeds` | Fetches the configured public feeds through the runtime acquisition path and reports current matches. |
| `make references-online` | Checks external links with Lychee. |

The corpus evaluator and feed screener accept `--root` and `--config`. Relative paths resolve from `--root`; absolute paths are accepted. The Make targets select [`config/dev.yaml`](config/dev.yaml) explicitly.

`make screen-feeds` and `make references-online` use the network. Feed screening reads public sources and does not require AWS credentials. Online reference checks require the `lychee` executable.

## Repository map

| Path | Contents |
| --- | --- |
| [`src/aws_public_change_feed/`](src/aws_public_change_feed/) | Feed acquisition, matching, candidates, releases, delivery, and recovery. |
| [`infra/`](infra/) | Terraform roots for bootstrap, persistent service resources, and isolated live exercises. |
| [`schemas/`](schemas/) and [`examples/`](examples/) | 8 strict JSON Schemas and one cross-file contract bundle. |
| [`config/`](config/) | Reviewed environment and matching policy. |
| [`corpus/`](corpus/) | Labeled announcements and accepted precision and recall thresholds. |
| [`site/`](site/) | GitHub Pages source, public MVP media, editable draw.io diagram, SVG export, and generated Slack example. |
| [`scripts/`](scripts/) and [`tests/`](tests/) | Validators, operator commands, regression tests, and service-mock tests. |
| [`docs/`](docs/) | Product goal, specification, ADRs, runbooks, agent-tooling notes, and supporting assets. |

## Documentation

- [Public system page](https://lilabrooks.github.io/aws-public-change-feed/): system diagram, processing summary, contract checks, and generated Slack output.
- [Product goal](docs/GOAL.md): scope, exclusions, quality bar, and completion criteria.
- [Architecture index](docs/architecture/README.md): 6 specification chapters, 20 accepted ADRs, and the schema-to-example map.
- [Operations runbook](docs/runbooks/operations.md): deployment, alarms, recovery, replay, rollback, and incident procedures.
- [Agent tooling notes](docs/agent-tooling.md): repository-specific AWS documentation and research boundaries.
- [Dev MVP walkthrough](docs/evidence/mvp-walkthrough.md): narrated evidence pack, slides, captions, transcript, and artifact hashes.

Changes to product scope, trust boundaries, identity, state ownership, delivery guarantees, or version policy require an ADR. Run `make check` before opening a pull request.

## License

Copyright 2026 Lila Brooks.

Licensed under the [Apache License 2.0](LICENSE). Redistributed copies and derivative works must preserve the attribution in [NOTICE](NOTICE).

References verified: 2026-08-29.
