# Production feed and matching policy

Owner decision: Accepted on 2026-09-01. The owner accepted a matcher revision
on 2026-10-06; see [Accepted 2026-10-05 revision](#accepted-2026-10-05-revision).

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
accepted revision below supersedes them:

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
no matcher, risk term, release bytes, or runtime behavior; the accepted
revision below removes three matcher literals.

## Accepted 2026-10-05 revision

The repository owner accepted this revision on 2026-10-06.

A live screen on 2026-10-05 (`make screen-feeds`) reported ten production
matches with no corpus label. Each was labeled from its runtime-normalized
text by what the announcement means, not by what the matcher detects. Six
announce changes to watched services. Four are false positives. An independent
review on 2026-10-05 reshaped the first draft, and the owner decided both
label questions it raised.

### Label decisions

- **Minor engine releases are version updates.** A new minor engine version
  that operators are advised to adopt is labeled `service-version-update`. It
  is also labeled `security` when its text cites CVE fixes. The owner applied
  this on 2026-10-05 to the two new PostgreSQL items and to the two earlier
  items that shared their security-only label: Aurora PostgreSQL 18.4 and RDS
  for MySQL 8.4.11. No configured term detects a minor release, so all four
  are recorded as false negatives rather than relabeled to fit the matcher.
- **The AWS Health version catalog is not an end-of-support announcement.**
  It lists RDS, EKS, and Lambda among its covered services, but it changes no
  support date. The owner decided this label.
- **The Amazon Connect Salesforce Lambda bulletin is not a Lambda service
  issue.** The vulnerable component is an Amazon Connect integration that is
  implemented as Lambda functions. Client-library bulletins such as the JDBC
  wrapper and Powertools for AWS Lambda remain positives, because those
  components exist to serve the watched service. The owner decided this label.

### Policy change

Three literals are removed. Each one had no historical true positive and at
least one false positive.

- **The `lambda` alias `Lambda function`.** It had no true positive. Its one
  false positive was bulletin 2026-115-AWS. It never matched the plural
  `Lambda functions`.
- **The end-of-support term `end-of-support`.** It had no true positive at
  all. Its three false positives all came from the AWS Health catalog launch.
  `end of support`, `end of life`, and `end-of-life` remain.
- **The version term `engine versions`.** Its only true positive was
  synthetic (`syn-rds-punctuation-variant`). Its two false positives were RDS
  instance-family launches that close with a pointer to "the specific engine
  versions that support these instance classes". The singular
  `engine version` remains. It has two synthetic true positives and no false
  positive.

The first draft instead excluded the instance-launch pointer through the
version rule's `none` list. The review measured removing the plural as a
smaller alternative. A `none` entry suppresses the whole rule for an
announcement, including clear version evidence elsewhere in its text. The review searched the corpus, today's 240 live announcements, and the 231
announcements the eight candidate feeds yield through the runtime path. In
all of that text, the plural occurred in a true-positive sense only in
synthetic items. Its other real occurrences named
DocumentDB and OpenSearch, which no configured service matches. The owner
chose removal on 2026-10-05.

Feeds, services, profiles, risk rules, and thresholds are otherwise unchanged.
The release built from this policy has a different identity from the active dev
release, so it must be published before the next live window. The delivery
preflight refuses an active release whose configuration differs from
`config/dev.yaml`.

### Revised exact inputs

| Input | SHA-256 |
| --- | --- |
| `config/dev.yaml` | `d022094fbf852be0735ceffcf03fc3ff538cd4cd591d6261a95b533f2ce5bd55` |
| `corpus/announcements.json` | `2fbef37cba1c0eaace88ca1e5aabebf6e88c50d7e3862d890ef07b908db56e0e` |
| `corpus/thresholds.json` | `bbd6eb5d530fe6e3e1653a0b2f15508e33df7ccdb45f817f1f66a7a8ed21b25a` |

### Revised corpus result

`make evaluate-corpus` passed on 2026-10-05 with 57 items: 42 historical and
15 synthetic. It reported 34 true positives, no false positives, and 5 false
negatives. Overall precision was 1.000 and recall 0.872, against floors of
0.950 and 0.800. All five misses are `rds/service-version-update`: the four
minor releases and the synthetic plural item.

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
| `rds/service-version-update` | 5 | 3 | 1.000 | 0.375 |
<!-- production-policy-pairs:end -->

### Revised evidence limits

**Pair coverage.** Five pairs have no historical positive and three have one.
The remaining four have two, three, five, and ten.

**Version-update recall.** `rds/service-version-update` now has five
historical positives and detects one of them, the SQL Server cumulative
updates. Two synthetic items supply the rest of its 0.375 recall. ADR-018 records pair
figures without gating on them, so this revision passes. The figure shows the
pair's thin recall that the earlier security-only labels had hidden. A term
for minor releases is the evident next candidate. It needs its own live
screen and evaluation before adoption.

**What guards the removals.** The global floors alone do not guard them.
Restoring each literal in isolation produced these results:

| Literal restored | Precision | Recall | Global gate |
| --- | --- | --- | --- |
| alias `Lambda function` | 0.971 | 0.872 | passes |
| plural `engine versions` | 0.946 | 0.897 | fails, by less than one false positive |
| term `end-of-support` | 0.919 | 0.872 | fails |

A test therefore names the four false-positive items as regression cases.
Each of the three restorations fails it on the item it guards. Every other
label stays under the global floors, as ADR-018 decides. A missed match on an
unrelated item does not fail the test.

**Hyphenated end-of-support wording.** Removing `end-of-support` narrows
recall on that wording for the end-of-support pairs. EKS and RDS have no
historical end-of-support positive, so the corpus cannot measure that loss in
either direction. AWS wording varies: one 2019 EKS documentation entry,
"Announcing discontinuation of support of Kubernetes 1.10", matches no
end-of-support term under either policy.

**What the screen can show.** The clean live screen after this revision reuses
the announcements that motivated it. It cannot show how the policy treats
future wording.

**Feed sources.** All four configured URLs returned their feeds without
redirects, with items published within the last four days. Eight other AWS
feeds were run through the runtime parser and matcher, and none was added:

- The three AWS blog feeds produced no match.
- The `docs.aws.amazon.com` history feeds need a host-allowlist change.
- Three of them exceed the 200-item parser limit, which rejects the whole
  feed.
- Their items mostly share one canonical URL, which the runtime uses as
  announcement identity. All 284 Aurora entries share one link, and 633 of
  634 RDS entries share another.

**M3 evidence.** The M3 readiness assessment of 2026-09-06 assessed the
2026-09-01 policy. Its delivery, recovery, load, rollback, and operations
evidence concerns mechanics rather than which announcements match, so it is
retained. The candidate counts it records came from the earlier policy. Its
corpus and pair-disposition row describes that policy, and this section
supersedes that row.
