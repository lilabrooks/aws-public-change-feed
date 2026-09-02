# Production feed and matching policy

Owner decision: Accepted on 2026-09-01.

## Decision

The reviewed policy in [`config/dev.yaml`](../../config/dev.yaml) is the
production-preflight feed and matching policy. It is retained byte-for-byte:
four feeds, the EKS, RDS, and Lambda service catalog, the
`standard-customer-stack` profile, and all four risk rules.

The global corpus floors remain 0.950 precision and 0.800 recall. Per-pair
overrides remain absent because the samples below cannot support stable
pair-specific floors. Release publication, deployment identity, inventory
identity, and the final readiness disposition remain part of the later M3
gate.

## Exact inputs

These digests bind the decision and its evaluation inputs:

| Input | SHA-256 |
| --- | --- |
| `config/dev.yaml` | `66545778ea430d4abb7cb27bd491e455a8719827be3c49e5cb33ffa0645f0bc1` |
| `corpus/announcements.json` | `036f150393a186ffc109442eea5eed1c27bbd38c9015f3a1e2060b92d9438694` |
| `corpus/thresholds.json` | `bbd6eb5d530fe6e3e1653a0b2f15508e33df7ccdb45f817f1f66a7a8ed21b25a` |

## Corpus result

`make evaluate-corpus` passed on 2026-09-01 with 47 items: 32 historical and
15 synthetic. It reported 29 true positives, no false positives, and no false
negatives. Overall precision and recall were both 1.000.

The selected disposition for every configured service and risk-type pair is
**retain**. Historical and synthetic columns count positive labels. Precision
and recall use the complete corpus. `undefined` means the corpus has no
expected or predicted positive for that pair.

<!-- production-policy-pairs:start -->
| Service and risk type | Historical positives | Synthetic positives | Precision | Recall |
| --- | ---: | ---: | ---: | ---: |
| `eks/breaking-change` | 0 | 0 | undefined | undefined |
| `eks/end-of-support` | 0 | 3 | 1.000 | 1.000 |
| `eks/security` | 2 | 0 | 1.000 | 1.000 |
| `eks/service-version-update` | 1 | 3 | 1.000 | 1.000 |
| `lambda/breaking-change` | 0 | 2 | 1.000 | 1.000 |
| `lambda/end-of-support` | 1 | 0 | 1.000 | 1.000 |
| `lambda/security` | 0 | 1 | 1.000 | 1.000 |
| `lambda/service-version-update` | 1 | 0 | 1.000 | 1.000 |
| `rds/breaking-change` | 0 | 0 | undefined | undefined |
| `rds/end-of-support` | 0 | 4 | 1.000 | 1.000 |
| `rds/security` | 7 | 0 | 1.000 | 1.000 |
| `rds/service-version-update` | 1 | 3 | 1.000 | 1.000 |
<!-- production-policy-pairs:end -->

## Evidence limits

Six pairs have no historical positive. Four more have one. The two remaining
pairs have two and seven historical positives. A perfect rate over one or two
positives is a thin observation, and a synthetic positive supplies contract
coverage rather than evidence of AWS wording in the field.

For a pair with no positive, the 32 historical items still exercise its false-
positive behavior. They cannot measure recall. The quiet persistent-dev feed
sample remains base-rate evidence and was not extended to obtain a positive
result.

The four configured feeds expose no deeper archive through the runtime
acquisition path used for corpus admission. Narrowing one thin pair is also not
an existing policy operation: service membership and risk rules are global, so
the current contract would remove a whole service or risk rule. The accepted
course keeps useful review coverage and carries the sample limit into the final
gate.

## Revisit conditions

Reopen the pair dispositions when the runtime acquisition path supplies a new
historical positive, a reviewed label exposes a false positive or false
negative, the enabled services or risk rules change, or the per-pair samples
become large enough to justify an override under
[ADR-018](../adr/018-corpus-evaluation-and-matching-thresholds.md).

Any matcher or risk-term change still runs the corpus evaluator and the live
feed screen required by the repository rules. This decision changes no matcher,
risk term, release bytes, or runtime behavior.
