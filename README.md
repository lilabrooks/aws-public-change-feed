# AWS Public Change Alerting

[![Status](https://img.shields.io/badge/status-contracts%20validated-00AA77)](#validation-status)
[![Repository quality](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/quality.yml/badge.svg?branch=main)](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/quality.yml)
[![Reference links](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/reference-links.yml/badge.svg?branch=main)](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/reference-links.yml)
[![Architecture page](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/pages.yml/badge.svg?branch=main)](https://lilabrooks.github.io/aws-public-change-feed/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

[![JSON Schema](https://img.shields.io/badge/contracts-JSON%20Schema-4B32C3?logo=json&logoColor=white)](schemas/)
[![Specs + ADRs](https://img.shields.io/badge/specs%20%2B%20ADRs-included-00AA77)](docs/architecture/README.md)
[![OpenAI Codex](https://img.shields.io/badge/built%20with-OpenAI%20Codex-412991?logo=openai&logoColor=white)](AGENTS.md)

AWS Public Change Alerting is a reference architecture for turning approved public AWS announcements into explainable, route-scoped Slack review candidates. It maps deterministic service and risk matches to potentially relevant environments without requiring customer-account access.

**[Read the public architecture page](https://lilabrooks.github.io/aws-public-change-feed/)** for the value proposition, design rationale, processing flow, system boundaries, and current evidence.

## Repository purpose

The repository is the authoritative architecture package. It contains:

- The product [goal and implementation milestones](docs/GOAL.md).
- A numbered [architecture specification](docs/architecture/README.md).
- Accepted [architecture decisions](docs/architecture/README.md#architecture-decision-records).
- Strict machine-readable [schemas](schemas/) and one canonical [example bundle](examples/).
- Semantic validators and regression tests for cross-document rules and deterministic identities.

Public announcements provide review evidence. They do not prove that an AWS account, environment, or resource is affected. Operators confirm applicability with their existing account-specific tools.

## Validation status

The `contracts validated` badge means the committed architecture artifacts pass the repository's automated checks:

- Each canonical example passes its paired JSON Schema.
- The complete example bundle passes projections, references, route, release-hash, identity, retention, and size checks.
- Regression tests confirm that rejected configuration and event-contract mutations fail validation.
- Python quality, YAML, local links, reference dates, the public page, and Git whitespace pass the same gate.

The Terraform roots and Python runtime remain implementation milestones. Deployment, live feed acquisition, Slack delivery, corpus quality, recovery, load, and production preflight still require executable evidence.

## Start here

1. Read the [public architecture page](https://lilabrooks.github.io/aws-public-change-feed/).
2. Read the [goal](docs/GOAL.md) for scope, milestones, and completion criteria.
3. Follow the [numbered specification](docs/architecture/README.md) in order.
4. Inspect the executable contracts in [`schemas/`](schemas/) and [`examples/`](examples/).

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
