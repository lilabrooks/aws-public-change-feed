# Production feed and matching policy

Owner decision: Accepted on 2026-09-01. A matcher revision is proposed on
2026-10-05 for owner review; see [Proposed 2026-10-05 revision](#proposed-2026-10-05-revision).

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

These digests bound the 2026-09-01 decision and its evaluation inputs. The
proposed revision below supersedes them once accepted:

| Input | SHA-256 |
| --- | --- |
| `config/dev.yaml` | `66545778ea430d4abb7cb27bd491e455a8719827be3c49e5cb33ffa0645f0bc1` |
| `corpus/announcements.json` | `036f150393a186ffc109442eea5eed1c27bbd38c9015f3a1e2060b92d9438694` |
| `corpus/thresholds.json` | `bbd6eb5d530fe6e3e1653a0b2f15508e33df7ccdb45f817f1f66a7a8ed21b25a` |

## Corpus result

`make evaluate-corpus` passed on 2026-09-01 with 47 items: 32 historical and
15 synthetic. It reported 29 true positives, no false positives, and no false
negatives. Overall precision and recall were both 1.000.

The 2026-09-01 disposition for every configured service and risk-type pair was
retain. Historical and synthetic columns count positive labels. Precision
and recall use the complete corpus. `undefined` means the corpus has no
expected or predicted positive for that pair.

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
feed screen required by the repository rules. The 2026-09-01 decision changed
no matcher, risk term, release bytes, or runtime behavior; the proposed
revision below changes three matcher literals.

## Proposed 2026-10-05 revision

A live screen on 2026-10-05 (`make screen-feeds`) reported ten production
matches with no corpus label. Each was labeled from its runtime-normalized
text, against the nearest existing corpus precedent. Six were true positives
and four were false positives. Labeled as reviewed, the unchanged policy
scored precision 0.854 (35 true positives, 6 false positives), below the 0.950
floor. The revision changes three matcher literals:

- **Remove the `Lambda function` alias from `lambda`.** No corpus positive
  depended on it. It fired on bulletin 2026-115-AWS, which concerns an Amazon
  Connect integration application implemented as Lambda functions rather than
  the Lambda service.
- **Remove the hyphenated `end-of-support` term.** It had no true positive in
  the corpus. Its only matches were three false positives from one item: AWS
  Health's version catalog launch, which lists RDS, EKS, and Lambda among the
  services it covers without changing a support date. The repository rule
  removes a term with no true positive and any false positive rather than
  excluding it case by case. `end of support`, `end of life`, and
  `end-of-life` remain.
- **Add `that support these instance classes` to the version rule's `none`
  list.** `engine versions` keeps a corpus positive, so it is excluded rather
  than removed. The phrase is AWS's closing pointer in RDS instance-family
  launches. It appeared identically in the M8a and R8a announcements and in no
  other text available to the review.

Feeds, services, profiles, risk rules, and thresholds are otherwise unchanged.
The release built from this policy has a different identity from the active dev
release, so it must be published before the next live window. The delivery
preflight refuses an active release whose configuration differs from
`config/dev.yaml`.

### Revised exact inputs

| Input | SHA-256 |
| --- | --- |
| `config/dev.yaml` | `a948faafd748552b5a2ec158502f532b01cab26779bc697cd9c9b55a4a27ce15` |
| `corpus/announcements.json` | `ed45f1af3051dd42dbda4441e1f208f768cc648213442220f4f0eb55b8f95089` |
| `corpus/thresholds.json` | `bbd6eb5d530fe6e3e1653a0b2f15508e33df7ccdb45f817f1f66a7a8ed21b25a` |

### Revised corpus result

`make evaluate-corpus` passed on 2026-10-05 with 57 items: 42 historical and
15 synthetic. It reported 35 true positives, no false positives, and no false
negatives. Overall precision and recall were both 1.000.

The selected disposition for every configured service and risk-type pair is
**retain**. Columns have the same meaning as in the 2026-09-01 table.

<!-- production-policy-pairs:start -->
| Service and risk type | Historical positives | Synthetic positives | Precision | Recall |
| --- | ---: | ---: | ---: | ---: |
| `eks/breaking-change` | 0 | 0 | undefined | undefined |
| `eks/end-of-support` | 0 | 3 | 1.000 | 1.000 |
| `eks/security` | 3 | 0 | 1.000 | 1.000 |
| `eks/service-version-update` | 2 | 3 | 1.000 | 1.000 |
| `lambda/breaking-change` | 0 | 2 | 1.000 | 1.000 |
| `lambda/end-of-support` | 1 | 0 | 1.000 | 1.000 |
| `lambda/security` | 1 | 1 | 1.000 | 1.000 |
| `lambda/service-version-update` | 1 | 0 | 1.000 | 1.000 |
| `rds/breaking-change` | 0 | 0 | undefined | undefined |
| `rds/end-of-support` | 0 | 4 | 1.000 | 1.000 |
| `rds/security` | 10 | 0 | 1.000 | 1.000 |
| `rds/service-version-update` | 1 | 3 | 1.000 | 1.000 |
<!-- production-policy-pairs:end -->

### Revised evidence limits

Five pairs now have no historical positive, four have one, and the remaining
three have two, three, and ten. `lambda/security` gained its first historical
positive.

The global floors alone do not guard these labels. Restoring each change in
isolation scored 0.972 for the alias, 0.946 for the exclusion, and 0.921 for
the hyphenated term: the alias regression passes the 0.950 floor, and the
exclusion fails it by less than one false positive. A test therefore requires
every corpus item to match exactly its labeled pairs. Each of the three
restorations fails that test on the item it affects.

Removing `end-of-support` narrows recall on hyphenated wording for the
end-of-support pairs. EKS and RDS have no historical end-of-support positive,
so the corpus cannot measure that loss in either direction. If AWS announces an
end of support for a watched service using only the hyphenated form, the
announcement will not match. That would be a reviewed false negative that
reopens this decision.

The screen also re-checked the feed sources. All four URLs returned their
feeds without redirects, with items published within the last four days.
Eight other AWS feeds were run through the runtime parser and matcher; none
was added. The three AWS blog feeds produced no match. The `docs.aws.amazon.com`
history feeds need a host-allowlist change. Three exceed the 200-item parser
limit, which rejects the whole feed. Their items mostly share one canonical
URL, which the runtime uses as announcement identity: all 284 Aurora entries
share one link, and 633 of 634 RDS entries share another.
