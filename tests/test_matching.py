import sys
import unittest
from dataclasses import replace
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.matching import (  # noqa: E402
    Announcement,
    RiskRule,
    ServiceDefinition,
    load_risk_rules,
    load_services,
    match_announcement,
)
from aws_public_change_feed.normalize import Span, fold, found_spans, normalize_text  # noqa: E402

BASE_RULE = RiskRule(
    id="test-rule",
    risk_type="end-of-support",
    priority="high",
    fields=("title", "summary"),
    any_terms=("end of support",),
    all_terms=(),
    none_terms=(),
)


def rule(**overrides):
    return replace(BASE_RULE, **overrides)


EKS = ServiceDefinition(
    id="eks", display_name="Amazon EKS", aliases=("Amazon EKS", "Amazon Elastic Kubernetes Service")
)
LAMBDA = ServiceDefinition(id="lambda", display_name="AWS Lambda", aliases=("AWS Lambda", "Lambda function"))


class NormalizeTests(unittest.TestCase):
    def test_whitespace_collapses_and_unicode_normalizes(self):
        self.assertEqual(normalize_text("Amazon EKS   ends\nsupport"), "Amazon EKS ends support")

    def test_fold_lowercases_after_normalizing(self):
        self.assertEqual(fold("  Amazon EKS  "), "amazon eks")

    def test_phrase_boundary_rejects_substring(self):
        self.assertEqual(found_spans("lambdas in programming", "lambda"), ())

    def test_phrase_boundary_accepts_punctuation_neighbours(self):
        self.assertEqual(found_spans("(aws lambda) changes", "aws lambda"), (Span(1, 11),))

    def test_every_occurrence_is_returned(self):
        self.assertEqual(found_spans("eks and eks", "eks"), (Span(0, 3), Span(8, 11)))

    def test_empty_needle_never_matches(self):
        self.assertEqual(found_spans("anything", ""), ())

    def test_unfolded_input_fails_silently_which_is_why_callers_must_fold(self):
        # Documents the trap rather than defending against it. Unfolded input
        # returns no spans instead of raising, so a caller that forgets to fold
        # sees a recall miss with no error. Worse, it is intermittent: it only
        # bites when case or Unicode differ, so it works by accident whenever
        # the feed's capitalization happens to match the configured alias.
        self.assertEqual(found_spans("AMAZON EKS ends support", "Amazon EKS"), ())
        self.assertEqual(found_spans(fold("AMAZON EKS ends support"), fold("Amazon EKS")), (Span(0, 10),))


class MatchingTests(unittest.TestCase):
    def test_service_and_risk_evidence_produces_a_match(self):
        results = match_announcement(Announcement("Amazon EKS announces end of support for a version"), [EKS], [rule()])
        self.assertEqual([result.pair for result in results], [("eks", "end-of-support")])

    def test_service_evidence_alone_does_not_match(self):
        results = match_announcement(Announcement("Amazon EKS reduces cluster creation time"), [EKS], [rule()])
        self.assertEqual(results, ())

    def test_risk_evidence_alone_does_not_match(self):
        results = match_announcement(Announcement("Amazon Redshift announces end of support"), [EKS], [rule()])
        self.assertEqual(results, ())

    def test_excluded_term_suppresses_the_match(self):
        results = match_announcement(
            Announcement("Amazon EKS end of support now available in more Regions"),
            [EKS],
            [rule(none_terms=("now available in",))],
        )
        self.assertEqual(results, ())

    def test_all_terms_must_all_be_present(self):
        rule_needing_both = rule(any_terms=(), all_terms=("end of support", "required action"))
        self.assertEqual(
            match_announcement(Announcement("Amazon EKS end of support"), [EKS], [rule_needing_both]),
            (),
        )
        results = match_announcement(
            Announcement("Amazon EKS end of support", "required action for cluster owners"),
            [EKS],
            [rule_needing_both],
        )
        self.assertEqual([result.pair for result in results], [("eks", "end-of-support")])

    def test_overlapping_evidence_alone_does_not_match(self):
        # The alias and the risk term cover the same characters, so the item
        # carries one piece of evidence read twice rather than two.
        overlapping = rule(any_terms=("aws lambda",))
        results = match_announcement(Announcement("AWS Lambda"), [LAMBDA], [overlapping])
        self.assertEqual(results, ())

    def test_evidence_in_separate_fields_matches(self):
        results = match_announcement(
            Announcement("AWS Lambda changes", "end of support for a runtime"), [LAMBDA], [rule()]
        )
        self.assertEqual([result.pair for result in results], [("lambda", "end-of-support")])

    def test_unconfigured_field_is_not_searched(self):
        title_only = rule(fields=("title",))
        results = match_announcement(
            Announcement("Amazon EKS update", "end of support for a version"), [EKS], [title_only]
        )
        self.assertEqual(results, ())

    def test_each_service_is_evaluated_independently(self):
        results = match_announcement(
            Announcement("Amazon EKS and AWS Lambda announce end of support"), [EKS, LAMBDA], [rule()]
        )
        self.assertEqual([result.pair for result in results], [("eks", "end-of-support"), ("lambda", "end-of-support")])

    def test_results_are_deterministically_ordered(self):
        announcement = Announcement("AWS Lambda and Amazon EKS announce end of support")
        forward = match_announcement(announcement, [EKS, LAMBDA], [rule()])
        reversed_services = match_announcement(announcement, [LAMBDA, EKS], [rule()])
        self.assertEqual(forward, reversed_services)

    def test_explainability_evidence_is_sorted_and_field_ordered(self):
        results = match_announcement(
            Announcement(
                "Amazon Elastic Kubernetes Service and Amazon EKS", "end of support and end of life for a version"
            ),
            [EKS],
            [rule(any_terms=("end of support", "end of life"))],
        )
        self.assertEqual(results[0].matched_aliases, ("Amazon EKS", "Amazon Elastic Kubernetes Service"))
        self.assertEqual(results[0].matched_terms, ("end of life", "end of support"))
        self.assertEqual(results[0].matched_fields, ("title", "summary"))

    def test_trailing_inflection_requires_its_own_configured_term(self):
        # Chapter 04 commits to exact phrases so a reviewer sees the literal
        # term that fired. The consequence is that a pluralized final word is
        # a distinct term, which is why config.yaml carries both forms.
        singular = rule(any_terms=("engine version",))
        self.assertEqual(
            match_announcement(Announcement("Amazon EKS lists engine versions"), [EKS], [singular]),
            (),
        )
        both = rule(any_terms=("engine version", "engine versions"))
        self.assertEqual(
            [
                result.pair
                for result in match_announcement(Announcement("Amazon EKS lists engine versions"), [EKS], [both])
            ],
            [("eks", "end-of-support")],
        )

    def test_a_following_word_does_not_defeat_a_configured_phrase(self):
        # The mirror case: the boundary falls after the configured phrase, so
        # no extra term is needed for "end of support dates".
        results = match_announcement(Announcement("Amazon EKS publishes end of support dates"), [EKS], [rule()])
        self.assertEqual([result.pair for result in results], [("eks", "end-of-support")])

    def test_rule_without_fields_is_skipped(self):
        self.assertEqual(match_announcement(Announcement("Amazon EKS end of support"), [EKS], [rule(fields=())]), ())


class ConfigurationLoadingTests(unittest.TestCase):
    def setUp(self):
        with (ROOT / "examples/config.yaml").open(encoding="utf-8") as handle:
            self.configuration = yaml.safe_load(handle)

    def test_services_load_from_the_canonical_configuration(self):
        services = load_services(self.configuration)
        self.assertEqual([service.id for service in services], ["eks", "lambda", "rds"])
        self.assertIn("Amazon Aurora", dict((service.id, service.aliases) for service in services)["rds"])

    def test_risk_rules_load_with_their_term_sets(self):
        rules = load_risk_rules(self.configuration)
        by_id = {entry.id: entry for entry in rules}
        self.assertIn("watched-service-end-of-support", by_id)
        self.assertEqual(by_id["watched-service-version-update"].none_terms, ("now available in", "available in the"))
        self.assertEqual(by_id["security-bulletin"].fields, ("title", "summary"))

    def test_risk_types_are_unique_across_rules(self):
        # Chapter 04 relies on this to make rule precedence unnecessary.
        risk_types = [entry.risk_type for entry in load_risk_rules(self.configuration)]
        self.assertEqual(len(risk_types), len(set(risk_types)))


if __name__ == "__main__":
    unittest.main()
