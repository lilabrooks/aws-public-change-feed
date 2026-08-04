"""Corpus evaluation: precision and recall against the approved thresholds.

Scoring works on ``(service_id, risk_type)`` pairs, as ADR-018 defines. A
corpus item may carry several expected pairs and each is scored independently,
so an item that names the right service under the wrong risk type produces one
false positive and one false negative rather than a single ambiguous result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .corpus import CorpusItem, Thresholds
from .matching import RiskRule, ServiceDefinition, match_announcement

__all__ = [
    "EvaluationReport",
    "PairOutcome",
    "Score",
    "evaluate_corpus",
    "format_report",
]


@dataclass(frozen=True, slots=True)
class Score:
    """Counts and rates for one pair, or for the corpus as a whole."""

    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0

    @property
    def predicted(self) -> int:
        return self.true_positives + self.false_positives

    @property
    def expected(self) -> int:
        return self.true_positives + self.false_negatives

    @property
    def precision(self) -> float | None:
        """``None`` when nothing was predicted; a rate over zero cases is undefined."""

        if self.predicted == 0:
            return None
        return self.true_positives / self.predicted

    @property
    def recall(self) -> float | None:
        """``None`` when nothing was expected."""

        if self.expected == 0:
            return None
        return self.true_positives / self.expected


@dataclass(frozen=True, slots=True)
class PairOutcome:
    """One pair's score, the threshold applied to it, and any failure.

    ``is_gating`` is true only when an explicit override names this pair.
    ADR-018 gates promotion on the global figures because per-pair rates are
    unstable on a small corpus: one miss against eight positives moves recall
    by 12.5 points. Non-gating pairs are still scored and reported, which is
    how the evidence for a future override accumulates.
    """

    pair: tuple[str, str]
    score: Score
    min_precision: float
    min_recall: float
    is_gating: bool = False

    @property
    def meets_thresholds(self) -> bool:
        precision = self.score.precision
        recall = self.score.recall
        if precision is not None and precision < self.min_precision:
            return False
        return not (recall is not None and recall < self.min_recall)


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    overall: Score
    pairs: tuple[PairOutcome, ...]
    min_precision: float
    min_recall: float
    item_count: int
    historical_count: int
    synthetic_count: int
    false_positive_items: tuple[tuple[str, tuple[str, str]], ...] = field(default=())
    false_negative_items: tuple[tuple[str, tuple[str, str]], ...] = field(default=())

    @property
    def failures(self) -> tuple[str, ...]:
        """Threshold failures, worst first. Empty means the gate passes."""

        problems: list[str] = []
        precision = self.overall.precision
        recall = self.overall.recall
        if precision is not None and precision < self.min_precision:
            problems.append(f"corpus precision {precision:.3f} is below the approved minimum {self.min_precision:.3f}")
        if recall is not None and recall < self.min_recall:
            problems.append(f"corpus recall {recall:.3f} is below the approved minimum {self.min_recall:.3f}")
        for outcome in self.pairs:
            if not outcome.is_gating or outcome.meets_thresholds:
                continue
            service_id, risk_type = outcome.pair
            problems.append(
                f"{service_id}/{risk_type} is below its override thresholds "
                f"(precision {_rate(outcome.score.precision)}, recall {_rate(outcome.score.recall)})"
            )
        return tuple(problems)

    @property
    def passed(self) -> bool:
        return not self.failures


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def evaluate_corpus(
    items: Sequence[CorpusItem],
    services: Sequence[ServiceDefinition],
    rules: Sequence[RiskRule],
    thresholds: Thresholds,
) -> EvaluationReport:
    """Run the matcher over the corpus and score it."""

    true_positives = 0
    false_positives = 0
    false_negatives = 0
    per_pair: dict[tuple[str, str], list[int]] = {}
    false_positive_items: list[tuple[str, tuple[str, str]]] = []
    false_negative_items: list[tuple[str, tuple[str, str]]] = []
    historical = 0
    synthetic = 0

    for item in items:
        if item.provenance == "historical":
            historical += 1
        else:
            synthetic += 1

        produced = {result.pair for result in match_announcement(item.announcement, services, rules)}
        expected = item.expected_pairs

        for pair in sorted(produced & expected):
            true_positives += 1
            per_pair.setdefault(pair, [0, 0, 0])[0] += 1
        for pair in sorted(produced - expected):
            false_positives += 1
            per_pair.setdefault(pair, [0, 0, 0])[1] += 1
            false_positive_items.append((item.id, pair))
        for pair in sorted(expected - produced):
            false_negatives += 1
            per_pair.setdefault(pair, [0, 0, 0])[2] += 1
            false_negative_items.append((item.id, pair))

    outcomes: list[PairOutcome] = []
    for pair in sorted(per_pair):
        counts = per_pair[pair]
        threshold = thresholds.for_pair(pair)
        outcomes.append(
            PairOutcome(
                pair=pair,
                score=Score(counts[0], counts[1], counts[2]),
                min_precision=threshold.min_precision,
                min_recall=threshold.min_recall,
                is_gating=thresholds.has_override(pair),
            )
        )

    return EvaluationReport(
        overall=Score(true_positives, false_positives, false_negatives),
        pairs=tuple(outcomes),
        min_precision=thresholds.global_threshold.min_precision,
        min_recall=thresholds.global_threshold.min_recall,
        item_count=len(items),
        historical_count=historical,
        synthetic_count=synthetic,
        false_positive_items=tuple(false_positive_items),
        false_negative_items=tuple(false_negative_items),
    )


def format_report(report: EvaluationReport) -> str:
    """Render the report chapter 04 requires promotion to record."""

    lines: list[str] = []
    lines.append(
        f"corpus: {report.item_count} items ({report.historical_count} historical, {report.synthetic_count} synthetic)"
    )
    lines.append(
        f"overall: precision {_rate(report.overall.precision)} recall {_rate(report.overall.recall)} "
        f"(tp {report.overall.true_positives}, fp {report.overall.false_positives}, "
        f"fn {report.overall.false_negatives})"
    )
    lines.append(f"approved thresholds: precision >= {report.min_precision:.3f} recall >= {report.min_recall:.3f}")

    if report.pairs:
        lines.append("per service and risk type:")
        for outcome in report.pairs:
            service_id, risk_type = outcome.pair
            if outcome.meets_thresholds:
                status = "ok"
            else:
                status = "BELOW OVERRIDE" if outcome.is_gating else "below global floor, recorded only"
            lines.append(
                f"  {service_id}/{risk_type}: precision {_rate(outcome.score.precision)} "
                f"recall {_rate(outcome.score.recall)} "
                f"(tp {outcome.score.true_positives}, fp {outcome.score.false_positives}, "
                f"fn {outcome.score.false_negatives}) {status}"
            )

    for label, entries in (
        ("false positives", report.false_positive_items),
        ("false negatives", report.false_negative_items),
    ):
        if entries:
            lines.append(f"{label}:")
            for item_id, (service_id, risk_type) in entries:
                lines.append(f"  {item_id}: {service_id}/{risk_type}")

    return "\n".join(lines)


def report_summary(report: EvaluationReport) -> Mapping[str, object]:
    """A machine-readable summary for promotion records."""

    return {
        "items": report.item_count,
        "historical_items": report.historical_count,
        "synthetic_items": report.synthetic_count,
        "precision": report.overall.precision,
        "recall": report.overall.recall,
        "min_precision": report.min_precision,
        "min_recall": report.min_recall,
        "passed": report.passed,
    }
