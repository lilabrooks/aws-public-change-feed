# ADR-018: Corpus evaluation and matching thresholds

- Status: Accepted
- Date: 2026-08-03
- Revision accepted: 2026-09-06

## Context

Chapter 04 requires every matcher change to be evaluated against a versioned historical corpus, and chapter 06 requires recorded per-service and per-risk precision and recall to meet "the approved thresholds". ADR-016 repeats the requirement for production preflight. No document states the numbers, so the acceptance criterion cannot be evaluated and the first milestone cannot complete.

The product is a review queue delivered to Slack. Its two failure modes are not symmetric. A queue carrying noise is abandoned, and an abandoned queue delivers nothing regardless of its recall. A missed announcement is serious but partially covered: operators still receive AWS communications through the console, account teams, and the same public feeds this service reads. The thresholds should reflect that asymmetry rather than treat the errors as equal.

Per-service and per-risk-type thresholds are the eventual goal, but setting them before any measurement exists means inventing numbers for pairs with no observed behavior. A small corpus also makes per-pair rates unstable: with eight labeled positives for a pair, one miss moves recall by 12.5 points.

## Decision

Set a global floor that every matcher promotion must clear:

- Precision at least 0.95.
- Recall at least 0.80.

Precision and recall are computed over matched `(service_id, risk_type)` pairs across the whole corpus. A corpus item may carry several expected pairs; each is scored independently.

Record per-pair precision and recall in every evaluation report, but gate promotion on the global figures. Support a named per-pair override mechanism so a pair that proves harder or more critical can carry its own numbers once measurements justify one. Overrides are absent until evidence supports them.

Store the thresholds in `corpus/thresholds.json` under a dedicated schema, not in `config.yaml`. The thresholds govern repository promotion, not runtime behavior; putting them in the release configuration would change release hashes on every threshold revision and imply the runtime reads them.

Keep the corpus as committed normalized JSON with expected-match labels, bounded at roughly 2 MB and enforced by a test. Store only the normalized fields the matcher reads. Raw feed bodies and HTML stay out of the repository.

Each corpus item declares its `provenance` as `historical` or `synthetic`. Synthetic items exercise the categories chapter 04 names — punctuation variants, Unicode, overlapping services, generic AWS prose, hard negatives — where a real announcement with the needed shape may not exist. Threshold evaluation reports both counts, because a corpus that is mostly synthetic measures the matcher against its author's expectations rather than against AWS's actual writing.

## Consequences

- The milestone-1 acceptance criterion becomes evaluable, and promotion has a mechanical gate.
- A precision-first bar means early matcher versions will miss relevant announcements. That is the accepted trade, and recall becomes the improvement target once precision holds.
- Per-pair figures are recorded from the first run, so the eventual per-pair thresholds are set from evidence rather than estimation.
- Threshold revisions do not touch release identity or any canonical example hash.
- A corpus dominated by synthetic items cannot by itself support a production claim. ADR-016 preflight still requires historical coverage of every enabled service and risk type.
- The 2 MB ceiling will eventually bind. Crossing it is a deliberate decision to revisit storage, not a silent repository growth.

## Rollback

If precision at 0.95 blocks useful matcher work while the observed noise in Slack proves tolerable, lower the floor to 0.90 and record the observed review burden that justified it. If the corpus reaches the size ceiling before covering the enabled services, move to fetched artifacts and record the credential and offline consequences for CI.

## Accepted 2026-09-06 revision: production corpus disposition

The repository owner accepted this revision on 2026-09-06.

For the exact production-like dev policy selected in L-40, historical coverage
means that every enabled `(service_id, risk_type)` pair has a reviewed
disposition in the promotion record. It does not mean that every pair must have
a historical positive. Recall is undefined for a pair with no labeled positive;
that pair is reported with its historical and synthetic counts and cannot gain
an invented per-pair threshold.

The selected corpus has 47 items: 32 historical and 15 synthetic. It yields 29
true positives, no false positives, and no false negatives. Six of the 12
enabled pairs have no historical positive, four have one, and the remaining two
have two and seven. The accepted global precision floor of 0.95 and recall
floor of 0.80 still govern promotion, and every pair remains visible in the
report.

The owner may accept this thin-data boundary for the reviewed deployment
without extending the sample merely to obtain a positive result. Revisit the
policy when a new historical item changes a pair's evidence, a matcher or label
changes, an enabled service or risk rule changes, a per-pair override is
proposed, the global floors fail, or observed Slack review burden contradicts
the precision-first choice.

## References

References verified: 2026-08-03.

- [Precision and recall](https://en.wikipedia.org/wiki/Precision_and_recall)
