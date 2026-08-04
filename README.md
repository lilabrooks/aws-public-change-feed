# AWS Public Change Alerting

[![Status](https://img.shields.io/badge/status-contracts%20%2B%20matching%20validated-00AA77)](#validation-status)
[![Repository quality](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/quality.yml/badge.svg?branch=main)](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/quality.yml)
[![Reference links](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/reference-links.yml/badge.svg?branch=main)](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/reference-links.yml)
[![Architecture page](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/pages.yml/badge.svg?branch=main)](https://lilabrooks.github.io/aws-public-change-feed/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

[![JSON Schema](https://img.shields.io/badge/contracts-JSON%20Schema-4B32C3?logo=json&logoColor=white)](schemas/)
[![Specs + ADRs](https://img.shields.io/badge/specs%20%2B%20ADRs-included-00AA77)](docs/architecture/README.md)

AWS Public Change Alerting turns approved public AWS announcements into explainable, route-scoped Slack review candidates. It maps deterministic service and risk matches to potentially relevant environments without requiring customer-account access.

**[Read the public architecture page](https://lilabrooks.github.io/aws-public-change-feed/)** for the value proposition, design rationale, processing flow, system boundaries, and current evidence.

## Repository purpose

The repository holds the service and its authoritative architecture package. The contracts are settled and validated; the runtime and infrastructure are the current build. It contains:

- The product [goal and implementation milestones](docs/GOAL.md).
- A numbered [architecture specification](docs/architecture/README.md).
- Accepted [architecture decisions](docs/architecture/README.md#architecture-decision-records).
- Strict machine-readable [schemas](schemas/) and one canonical [example bundle](examples/).
- The [matching runtime](src/aws_public_change_feed/) and the [labeled corpus](corpus/) its quality is measured against.
- Semantic validators and regression tests for cross-document rules and deterministic identities.

Public announcements provide review evidence. They do not prove that an AWS account, environment, or resource is affected. Operators confirm applicability with their existing account-specific tools.

## Validation status

The `contracts + matching validated` badge means the committed artifacts and the implemented matcher pass the repository's automated checks:

- Each canonical example passes its paired JSON Schema.
- The complete example bundle passes projections, references, route, release-hash, identity, retention, and size checks.
- Regression tests confirm that rejected configuration and event-contract mutations fail validation.
- Python quality, YAML, local links, reference dates, the public page, and Git whitespace pass the same gate.
- Deterministic matching scores at or above the approved thresholds against the labeled corpus.

Matching is the first implemented runtime package. Its corpus mixes real announcements taken from the four configured feeds with authored items covering shapes the recent feed window did not contain. No end-of-support announcement appeared in that window, so recall for that risk type rests on authored items alone. The Terraform roots and the remaining runtime packages are implementation milestones. Deployment, live feed acquisition, Slack delivery, recovery, load, and production preflight still require executable evidence.

## Start here

1. Read the [public architecture page](https://lilabrooks.github.io/aws-public-change-feed/).
2. Read the [goal](docs/GOAL.md) for scope, milestones, and completion criteria.
3. Follow the [numbered specification](docs/architecture/README.md) in order.
4. Inspect the executable contracts in [`schemas/`](schemas/) and [`examples/`](examples/).
5. Run `make evaluate-corpus` to score matching against [`corpus/`](corpus/).

## Local validation

Python 3.12 or newer is required.

```bash
python3.12 -m venv .venv
. .venv/bin/activate
make install
make check
```

Run the network-backed reference check separately:

```bash
make references-online
```

References verified: 2026-07-13.

## License

Copyright 2026 Lila Brooks.

Licensed under the [Apache License 2.0](LICENSE). Redistributed copies and derivative works must preserve the attribution required by the license, including [NOTICE](NOTICE).
