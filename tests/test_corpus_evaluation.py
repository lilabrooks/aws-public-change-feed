import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import evaluate_corpus as harness  # noqa: E402

from aws_public_change_feed.corpus import (  # noqa: E402
    CorpusItem,
    ExpectedMatch,
    Override,
    Threshold,
    Thresholds,
    load_corpus,
    load_thresholds,
)
from aws_public_change_feed.evaluation import Score, evaluate_corpus, format_report  # noqa: E402
from aws_public_change_feed.matching import load_risk_rules, load_services  # noqa: E402

CORPUS_PATH = ROOT / "corpus/announcements.json"
THRESHOLDS_PATH = ROOT / "corpus/thresholds.json"


BASE_ITEM = CorpusItem(
    id="example",
    provenance="synthetic",
    category="positive",
    canonical_url="https://corpus.invalid/synthetic/example",
    title="Amazon EKS announces end of support for a version",
    summary="",
    expected_matches=(ExpectedMatch("eks", "end-of-support"),),
)


def item(**overrides):
    return replace(BASE_ITEM, **overrides)


class ScoreTests(unittest.TestCase):
    def test_rates_are_undefined_rather_than_zero_when_nothing_applies(self):
        self.assertIsNone(Score().precision)
        self.assertIsNone(Score().recall)

    def test_rates_are_computed_from_counts(self):
        score = Score(true_positives=3, false_positives=1, false_negatives=1)
        precision = score.precision
        recall = score.recall
        assert precision is not None and recall is not None
        self.assertAlmostEqual(precision, 0.75)
        self.assertAlmostEqual(recall, 0.75)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        with (ROOT / "examples/config.yaml").open(encoding="utf-8") as handle:
            configuration = yaml.safe_load(handle)
        self.services = load_services(configuration)
        self.rules = load_risk_rules(configuration)
        self.thresholds = Thresholds(global_threshold=Threshold(0.95, 0.8))

    def evaluate(self, items, thresholds=None):
        return evaluate_corpus(items, self.services, self.rules, thresholds or self.thresholds)

    def test_a_correct_match_scores_as_a_true_positive(self):
        report = self.evaluate([item()])
        self.assertEqual(report.overall.true_positives, 1)
        self.assertTrue(report.passed)

    def test_an_unexpected_match_scores_as_a_false_positive(self):
        report = self.evaluate([item(expected_matches=())])
        self.assertEqual(report.overall.false_positives, 1)
        self.assertFalse(report.passed)
        self.assertIn("precision", report.failures[0])

    def test_a_missed_match_scores_as_a_false_negative(self):
        report = self.evaluate(
            [
                item(
                    id="silent",
                    title="Amazon EKS improves cluster startup",
                    expected_matches=(ExpectedMatch("eks", "end-of-support"),),
                )
            ]
        )
        self.assertEqual(report.overall.false_negatives, 1)
        self.assertFalse(report.passed)
        self.assertIn("recall", report.failures[0])

    def test_wrong_risk_type_counts_as_both_error_kinds(self):
        report = self.evaluate([item(expected_matches=(ExpectedMatch("eks", "security"),))])
        self.assertEqual(report.overall.false_positives, 1)
        self.assertEqual(report.overall.false_negatives, 1)

    def test_provenance_counts_are_reported(self):
        report = self.evaluate([item(), item(id="real", provenance="historical")])
        self.assertEqual((report.historical_count, report.synthetic_count), (1, 1))

    def test_a_pair_below_the_floor_does_not_gate_without_an_override(self):
        # ADR-018 gates on the global figures; per-pair rates are recorded.
        items = [item(id=f"positive-{index}") for index in range(20)]
        items.append(
            item(
                id="missed",
                title="Amazon RDS improves failover",
                expected_matches=(ExpectedMatch("rds", "end-of-support"),),
            )
        )
        report = self.evaluate(items)
        rds_outcome = next(outcome for outcome in report.pairs if outcome.pair == ("rds", "end-of-support"))
        self.assertFalse(rds_outcome.meets_thresholds)
        self.assertFalse(rds_outcome.is_gating)
        self.assertTrue(report.passed)

    def test_an_override_makes_its_pair_gate(self):
        items = [item(id=f"positive-{index}") for index in range(20)]
        items.append(
            item(
                id="missed",
                title="Amazon RDS improves failover",
                expected_matches=(ExpectedMatch("rds", "end-of-support"),),
            )
        )
        thresholds = Thresholds(
            global_threshold=Threshold(0.95, 0.8),
            overrides=(Override("rds", "end-of-support", 0.95, 0.8, "critical pair"),),
        )
        report = self.evaluate(items, thresholds)
        self.assertFalse(report.passed)
        self.assertIn("rds/end-of-support", report.failures[0])

    def test_report_records_the_figures_promotion_must_capture(self):
        text = format_report(self.evaluate([item()]))
        self.assertIn("overall: precision", text)
        self.assertIn("per service and risk type:", text)
        self.assertIn("approved thresholds:", text)


class CommittedCorpusTests(unittest.TestCase):
    def test_corpus_passes_its_schema(self):
        self.assertEqual(harness.validate_document(CORPUS_PATH, ROOT / "schemas/corpus.schema.json"), [])

    def test_thresholds_pass_their_schema(self):
        self.assertEqual(harness.validate_document(THRESHOLDS_PATH, ROOT / "schemas/corpus-thresholds.schema.json"), [])

    def test_committed_thresholds_match_the_accepted_decision(self):
        thresholds = load_thresholds(THRESHOLDS_PATH)
        self.assertEqual(thresholds.global_threshold.min_precision, 0.95)
        self.assertEqual(thresholds.global_threshold.min_recall, 0.80)

    def test_item_ids_are_unique(self):
        self.assertEqual(harness.duplicate_ids(CORPUS_PATH), [])

    def test_corpus_stays_within_its_size_ceiling(self):
        self.assertLessEqual(CORPUS_PATH.stat().st_size, harness.MAX_CORPUS_BYTES)

    def test_corpus_covers_the_categories_chapter_04_requires(self):
        categories = {entry.category for entry in load_corpus(CORPUS_PATH)}
        required = {
            "positive",
            "hard-negative",
            "overlapping-services",
            "generic-prose",
            "punctuation-variant",
            "unicode",
            "html",
            "edit",
            "security-guidance",
        }
        self.assertEqual(required - categories, set())

    def test_expected_services_and_risk_types_exist_in_the_configuration(self):
        with (ROOT / "examples/config.yaml").open(encoding="utf-8") as handle:
            configuration = yaml.safe_load(handle)
        service_ids = {service.id for service in load_services(configuration)}
        risk_types = {entry.risk_type for entry in load_risk_rules(configuration)}
        for entry in load_corpus(CORPUS_PATH):
            for expected in entry.expected_matches:
                self.assertIn(expected.service_id, service_ids, entry.id)
                self.assertIn(expected.risk_type, risk_types, entry.id)

    def test_committed_corpus_meets_the_approved_thresholds(self):
        result = subprocess.run(
            [sys.executable, "scripts/evaluate_corpus.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class CorpusRejectionTests(unittest.TestCase):
    """Every rejected corpus mutation keeps a regression case, per AGENTS.md."""

    def mutated(self, mutate):
        with CORPUS_PATH.open(encoding="utf-8") as handle:
            document = json.load(handle)
        mutate(document)
        with (ROOT / "schemas/corpus.schema.json").open(encoding="utf-8") as handle:
            schema = json.load(handle)
        validator = jsonschema.Draft202012Validator(schema)
        return list(validator.iter_errors(document))

    def test_unknown_field_is_rejected(self):
        self.assertTrue(self.mutated(lambda document: document["items"][0].update({"weight": 2})))

    def test_missing_provenance_is_rejected(self):
        self.assertTrue(self.mutated(lambda document: document["items"][0].pop("provenance")))

    def test_unknown_category_is_rejected(self):
        self.assertTrue(self.mutated(lambda document: document["items"][0].update({"category": "misc"})))

    def test_non_https_url_is_rejected(self):
        self.assertTrue(
            self.mutated(lambda document: document["items"][0].update({"canonical_url": "http://corpus.invalid/x"}))
        )

    def test_blank_title_is_rejected(self):
        self.assertTrue(self.mutated(lambda document: document["items"][0].update({"title": "   "})))

    def test_duplicate_expected_match_is_rejected(self):
        def duplicate(document):
            entry = document["items"][0]
            entry["expected_matches"] = [
                {"service_id": "eks", "risk_type": "end-of-support"},
                {"service_id": "eks", "risk_type": "end-of-support"},
            ]

        self.assertTrue(self.mutated(duplicate))

    def test_uppercase_service_id_is_rejected(self):
        def uppercase(document):
            document["items"][0]["expected_matches"] = [{"service_id": "EKS", "risk_type": "end-of-support"}]

        self.assertTrue(self.mutated(uppercase))

    def test_duplicate_item_id_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            with CORPUS_PATH.open(encoding="utf-8") as handle:
                document = json.load(handle)
            document["items"].append(dict(document["items"][0]))
            path = Path(directory) / "announcements.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertTrue(harness.duplicate_ids(path))


class ThresholdRejectionTests(unittest.TestCase):
    def mutated(self, mutate):
        with THRESHOLDS_PATH.open(encoding="utf-8") as handle:
            document = json.load(handle)
        mutate(document)
        with (ROOT / "schemas/corpus-thresholds.schema.json").open(encoding="utf-8") as handle:
            schema = json.load(handle)
        return list(jsonschema.Draft202012Validator(schema).iter_errors(document))

    def test_rate_above_one_is_rejected(self):
        self.assertTrue(self.mutated(lambda document: document["global"].update({"min_precision": 1.5})))

    def test_negative_rate_is_rejected(self):
        self.assertTrue(self.mutated(lambda document: document["global"].update({"min_recall": -0.1})))

    def test_missing_recall_is_rejected(self):
        self.assertTrue(self.mutated(lambda document: document["global"].pop("min_recall")))

    def test_override_without_reason_is_rejected(self):
        def override(document):
            document["overrides"] = [
                {"service_id": "eks", "risk_type": "end-of-support", "min_precision": 0.9, "min_recall": 0.9}
            ]

        self.assertTrue(self.mutated(override))


if __name__ == "__main__":
    unittest.main()
