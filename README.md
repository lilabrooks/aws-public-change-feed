# AWS Public Change Alerting

[![Repository quality](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/quality.yml/badge.svg?event=pull_request)](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/quality.yml)
[![Repository security](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/security.yml/badge.svg?event=pull_request)](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/security.yml)
[![Public site](https://github.com/lilabrooks/aws-public-change-feed/actions/workflows/pages.yml/badge.svg?branch=main)](https://lilabrooks.github.io/aws-public-change-feed/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

AWS Public Change Alerting watches approved AWS RSS and Atom feeds for changes that may need review. It matches announcement text against your service catalog and risk rules, maps each result to configured environments, and sends a route-scoped candidate to Slack.

Matches are **potentially relevant**. The service uses public announcements and static environment profiles; operators confirm actual account or resource impact with their existing AWS tools.

[![Processing overview: approved AWS feeds pass through the watcher, matching and routing, DynamoDB, SQS FIFO, and Slack delivery; recovery writes back to DynamoDB.](docs/assets/readme-overview.svg)](https://lilabrooks.github.io/aws-public-change-feed/)

[Open the system diagram, walkthrough, and generated Slack message.](https://lilabrooks.github.io/aws-public-change-feed/)

## Contents

- [Project status](#project-status)
- [Run locally](#run-locally)
- [Deploy your own copy](#deploy-your-own-copy)
- [How it works](#how-it-works)
- [Repository map](#repository-map)
- [Documentation](#documentation)
- [License](#license)

## Project status

The application, Terraform roots, contracts, and test suite are implemented. The exact reviewed dev deployment passed its production-readiness gate for one environment, one Slack destination, four feeds, three services, four risk rules, and 300 deliveries per hour.

For that gate, the owner selected the current 4-feed, 3-service, 4-risk-rule policy unchanged.

That evidence applies only to the recorded dev deployment. Six of its 12 enabled service/risk pairs have no historical positive, Slack delivery has an explicit `delivery_unknown` state, and the recovery proof covers stopped restore and restart rather than live service on restored tables. [Read the full assessment and its limits.](docs/production-readiness.md)

Dev runs only during supervised short windows and is parked between tests. [L-59](https://github.com/lilabrooks/aws-public-change-feed/issues/205) must pass before unattended use.

<details>
<summary>Milestone history</summary>

| Milestone | State | Recorded result |
| --- | --- | --- |
| [D0](https://github.com/lilabrooks/aws-public-change-feed/milestone/1) | Closed | First live public-feed candidate reached Slack and DynamoDB recorded `posted`. |
| [M1](https://github.com/lilabrooks/aws-public-change-feed/milestone/2) | Closed | Scheduled delivery, recovery, load, and alarm exercises completed in dev. |
| [M2](https://github.com/lilabrooks/aws-public-change-feed/milestone/3) | Closed | Retention, retirement, saved-response replay, and named recovery repairs completed. |
| [M3](https://github.com/lilabrooks/aws-public-change-feed/milestone/4) | Closed | The exact dev deployment passed its bounded production-readiness review. |
| [M4](https://github.com/lilabrooks/aws-public-change-feed/milestone/5) | Supervised proof passed | Fixed observation, early parking, and deadline cleanup after local waiter loss passed. |

The [goal](docs/GOAL.md), [readiness evidence](docs/evidence/m3-production-readiness-assessment-2026-09-06.md), and [September 8 live-window record](docs/evidence/l57-l58-supervised-live-window-2026-09-08.md) carry the detailed results. GitHub Issues and milestones hold current backlog state.

</details>

## Run locally

Python 3.12 or newer is required.

```bash
git clone https://github.com/lilabrooks/aws-public-change-feed.git
cd aws-public-change-feed
python3.12 -m venv .venv
. .venv/bin/activate
make install
make check
```

`make check` runs formatting checks, Python and YAML lint, type checks, contract validation, corpus scoring, tests, and whitespace checks. It also validates Terraform and runs TFLint when those tools are installed. CI requires both tools and tests Terraform 1.10.0 as the minimum supported release.

| Command | Use |
| --- | --- |
| `make evaluate-corpus` | Score the matcher against the labeled corpus and approved thresholds. |
| `make screen-feeds` | Fetch the configured public feeds through the runtime acquisition path. |
| `make references-online` | Check external links with Lychee. |
| `make terraform-clean` | Remove generated `.terraform` directories from all four Terraform roots. |

`make screen-feeds` uses the public network. `make references-online` uses the network and requires Lychee. None of these commands deploy infrastructure or send a Slack message.

The corpus evaluator and feed screener accept `--root` and `--config`. Relative paths resolve from `--root`; absolute paths are accepted. The Make targets select [`config/dev.yaml`](config/dev.yaml).

## Deploy your own copy

Self-hosting is a reviewed AWS deployment, with automatic triggers kept off until the Slack preflight passes. It creates billable resources and stores internal environment metadata in AWS. The tracked deployment files describe this repository's dev environment, so a fork needs its own identifiers and evidence.

You need:

- Python 3.12+, Terraform `>= 1.10.0, < 2.0.0`, TFLint, and an authenticated AWS CLI.
- An AWS identity allowed to apply the bootstrap and central roots, including their IAM resources.
- Globally unique S3 bucket names, an AWS account and Region, and an operational-notification email address.
- A Slack app with an incoming webhook for the target channel. Slack treats the webhook URL as a secret.

### 1. Set your deployment and matching policy

Use [`examples/deployment.yaml`](examples/deployment.yaml) as the field guide, then edit these tracked inputs:

- [`infra/central/deployment.yaml`](infra/central/deployment.yaml): deployment ID, Region, configuration bucket, notification aliases, Slack routes, environment metadata, feed and Slack host allowlists, and scale limits.
- [`config/dev.yaml`](config/dev.yaml): feeds, services, aliases, profiles, environment policy, and risk rules. Every deployment environment needs one matching policy entry.
- [`infra/bootstrap/backend.tf`](infra/bootstrap/backend.tf) and [`infra/central/backend.tf`](infra/central/backend.tf): literal state bucket and Region. Terraform backends can't read variables.

The bootstrap buckets are derived as `apcf-state-<deployment_id>` and `apcf-concurrency-<deployment_id>`. Pick a short deployment ID that produces valid, globally unique names, or revise the naming rules and their tests. The configuration bucket must also be globally unique. Keep the deployment feed-host allowlist equal to the host set in the runtime configuration.

Environment account IDs and Regions are static review context. They grant no customer-account access.

Treat [`infra/live-control/`](infra/live-control/) as an optional, deployment-specific root. It contains fixed account, Region, resource, and IAM values for the recorded dev environment and needs a reviewed port before another deployment can use it. The core bootstrap and central roots do not depend on it.

Run the full local gate after editing:

```bash
REQUIRE_TERRAFORM=1 REQUIRE_TFLINT=1 make check
```

### 2. Create and migrate the remote-state bucket

The bootstrap root creates the bucket that later stores its own state. Follow the comment in [`infra/bootstrap/backend.tf`](infra/bootstrap/backend.tf): temporarily comment out its backend block, then run a local-state apply with your values.

```bash
terraform -chdir=infra/bootstrap init -input=false
terraform -chdir=infra/bootstrap plan -input=false -out=bootstrap.tfplan \
  -var='deployment_id=<globally-unique-id>' \
  -var='region=<aws-region>'
terraform -chdir=infra/bootstrap apply bootstrap.tfplan
terraform -chdir=infra/bootstrap output state_bucket_name
```

Restore the backend block with the new bucket and Region, update the central backend to match, then migrate bootstrap state:

```bash
terraform -chdir=infra/bootstrap init -migrate-state -input=false
```

Review the generated backend policy and grant it to the principal that will run Terraform. The [architecture baseline](docs/adr/006-terraform-and-python-implementation-baseline.md) defines the state and lockfile permissions.

The bootstrap root also creates the versioned ADR-019 concurrency-test bucket and an `apcf_concurrency_test` IAM user. It creates no access key. Both are part of the repository's real-S3 promotion test contract.

### 3. Apply the central foundation with triggers disabled

Keep private values in an untracked file outside the repository. Its `operational_sns_subscription_endpoints` keys must exactly match the aliases in `deployment.yaml`.

```json
{
  "operational_sns_subscription_endpoints": {
    "primary-email": "operator@example.com"
  },
  "delivery_triggers_enabled": false,
  "reconciler_trigger_enabled": false
}
```

Initialize, review a saved plan, and apply those exact bytes:

```bash
terraform -chdir=infra/central init -input=false
terraform -chdir=infra/central plan -input=false \
  -var-file=/absolute/private/path/central.tfvars.json \
  -out=central-foundation.tfplan
terraform -chdir=infra/central apply central-foundation.tfplan
```

This first apply creates the configuration bucket, DynamoDB tables, queues, roles, notification topic, and Slack credential container. Lambda functions remain absent until exact package inputs are supplied. Confirm the SNS subscription before depending on alarm email.

### 4. Add the Slack credential

Create an [incoming webhook in Slack](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/) for the channel named by the route. Set that route's `credential_secret_id` in `deployment.yaml` before the central apply.

After Terraform creates the container, store the **whole webhook URL** as its value:

- For `secrets_manager`, add a value to the named secret with the AWS console or [`PutSecretValue`](https://docs.aws.amazon.com/cli/latest/reference/secretsmanager/put-secret-value.html).
- For `ssm_parameter_store`, replace Terraform's placeholder with a `SecureString` value using the AWS console or [`PutParameter`](https://docs.aws.amazon.com/cli/latest/reference/ssm/put-parameter.html).

Keep the webhook out of Git, Terraform variables, shell history, logs, and saved plans.

<details>
<summary>Bot-token mode</summary>

The deployment schema also supports `bot_token`. Set `workspace_id` and `bot_token_secret_id`, use `channel_id` on each route, and remove `approved_webhook_hosts` plus per-route `credential_secret_id`. Each `destination_key` must be the lowercase `<workspace_id>-<channel_id>` pair. Store the whole bot token in the deployment's selected secret store.

</details>

### 5. Publish the application package and create the Lambdas

Build the package, then publish it under the central root's release-publisher role:

```bash
python3 scripts/build_lambda_package.py --output build/slack-worker.zip
python3 scripts/publish_lambda_artifact.py \
  --bucket <config-bucket> \
  --prefix <application-artifact-prefix> \
  --package build/slack-worker.zip
```

Record the returned digest, S3 VersionId, and checksum. Set the worker, watcher, and dispatcher digest/VersionId pairs to that object, then set `worker_artifact_checksum_sha256`. Set the reconciler pair and `reconciler_artifact_checksum_sha256` from the same publication. Re-plan and apply with both trigger flags still `false`.

### 6. Publish configuration, test Slack, then enable schedules

Follow the operations runbook in this order:

1. [Capture fresh central Terraform outputs](docs/runbooks/operations.md#terraform-output-capture-and-recovery).
2. [Preview and apply one immutable configuration release](docs/runbooks/operations.md#configuration-release-publication). Only `status=completed` makes the release usable.
3. [Run the disabled-trigger delivery preflight](docs/runbooks/operations.md#application-package-rollout-and-rollback). Its apply step can fetch live feeds and send one real candidate to Slack when the fixed sample contains a bounded match.
4. Confirm the candidate in the named Slack channel. Keep a quiet or refused sample as its actual result; do not extend it to obtain a match.
5. Review and apply a second central plan with `delivery_triggers_enabled=true`. Enable `reconciler_trigger_enabled` only after completing its separate checks in the same runbook.

Preserve each reviewed plan, digest, release ID, package version, preflight result, and trigger readback. The [operations runbook](docs/runbooks/operations.md) owns rollback, recovery, delivery reconciliation, feed changes, and retirement procedures.

## How it works

1. The watcher fetches approved public feeds with URL, DNS, TLS, redirect, size, and parser controls.
2. It normalizes items and merges duplicate announcement URLs across feeds.
3. Deterministic service aliases and risk phrases match title and summary text.
4. Static profiles map each match to potentially relevant environments and Slack routes.
5. DynamoDB stores the candidate and delivery request before the feed checkpoint advances.
6. The dispatcher sends due work through SQS FIFO; the worker posts to Slack and records the outcome.
7. The reconciler repairs eligible work and changes expired send leases to `delivery_unknown`.

<details>
<summary>Why DynamoDB and SQS are both required</summary>

DynamoDB is the delivery system of record. It keeps candidates, outbox work, dedupe state, retries, and ambiguous outcomes after SQS's deduplication window ends. SQS carries ready work and preserves per-destination ordering. A known `posted` record suppresses another Slack call; an uncertain call stops at `delivery_unknown` until an operator checks Slack.

[ADR-004](docs/adr/004-explicit-slack-delivery-guarantees.md) defines Slack outcomes. [ADR-007](docs/adr/007-central-slack-delivery-queue-and-worker.md) defines the outbox and queue boundary.

</details>

## Repository map

| Path | Contents |
| --- | --- |
| [`src/aws_public_change_feed/`](src/aws_public_change_feed/) | Feed acquisition, matching, candidates, releases, delivery, and recovery. |
| [`infra/`](infra/) | Bootstrap, central service, isolated preflight, and deployment-specific live-control Terraform. |
| [`config/`](config/) and [`corpus/`](corpus/) | Reviewed policy, labeled announcements, and matching thresholds. |
| [`schemas/`](schemas/) and [`examples/`](examples/) | Strict contracts and the canonical cross-file example bundle. |
| [`scripts/`](scripts/) and [`tests/`](tests/) | Validators, operator commands, regression tests, and service-mock tests. |
| [`docs/`](docs/) | Product goal, specification, ADRs, runbooks, evidence, and supporting assets. |
| [`site/`](site/) | GitHub Pages source, diagrams, generated Slack sample, and walkthrough media. |

## Documentation

- [Product goal](docs/GOAL.md): outcome, scope, quality bar, and current evidence.
- [Architecture index](docs/architecture/README.md): six specification chapters, 26 accepted ADRs, and the contract map.
- [Operations runbook](docs/runbooks/operations.md): publication, preflight, rollback, replay, alarms, and recovery.
- [Live windows and parking](docs/runbooks/live-window.md): the current dev-only supervised control path and retained costs.
- [Repository checks](docs/repository-file-checks.md): local and CI checks, tool requirements, and security scanning.
- [Public system page](https://lilabrooks.github.io/aws-public-change-feed/): diagram, walkthrough, and generated Slack output.

Changes to product scope, trust boundaries, identity, state ownership, delivery guarantees, or version policy require an ADR. Run `make check` before opening a pull request.

## License

Copyright 2026 Lila Brooks.

Licensed under the [Apache License 2.0](LICENSE). Redistributed copies and derivative works must preserve the attribution in [NOTICE](NOTICE).

References verified: 2026-09-08.
