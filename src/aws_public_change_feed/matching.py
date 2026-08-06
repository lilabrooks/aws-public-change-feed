"""Deterministic service and risk matching.

Implements the matching rules in specification chapter 04:

1. Find service aliases using normalized phrase boundaries.
2. Find positive and excluded risk terms using the same boundary rules.
3. Evaluate ``any``, ``all``, and ``none``.
4. Require at least one service evidence span and one positive risk evidence
   span that do not overlap.
5. Produce one service/risk result with normalized lexical ordering for
   aliases and terms, configured field ordering, and the rule ID.

The matcher takes no configuration of its own. Everything it reads comes from
an already-validated release, so a rule change is a configuration change.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .identity import evidence_order
from .normalize import Span, fold, found_spans

__all__ = [
    "Announcement",
    "MatchResult",
    "RiskRule",
    "ServiceDefinition",
    "load_risk_rules",
    "load_services",
    "match_announcement",
]

FieldName = Literal["title", "summary"]

FIELD_NAMES: tuple[FieldName, ...] = ("title", "summary")


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    """A configured service and the aliases that evidence it."""

    id: str
    display_name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RiskRule:
    """A configured risk rule and its term sets."""

    id: str
    risk_type: str
    priority: str
    fields: tuple[FieldName, ...]
    any_terms: tuple[str, ...]
    all_terms: tuple[str, ...]
    none_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Announcement:
    """The normalized fields the matcher reads."""

    title: str
    summary: str = ""

    def field_value(self, name: FieldName) -> str:
        return self.title if name == "title" else self.summary


@dataclass(frozen=True, slots=True)
class MatchResult:
    """One service and risk match with its explainability evidence."""

    service_id: str
    service_display_name: str
    rule_id: str
    risk_type: str
    priority: str
    matched_aliases: tuple[str, ...] = field(default=())
    matched_terms: tuple[str, ...] = field(default=())
    matched_fields: tuple[FieldName, ...] = field(default=())

    @property
    def pair(self) -> tuple[str, str]:
        """The ``(service_id, risk_type)`` pair evaluation scores."""

        return (self.service_id, self.risk_type)


def _as_field_names(values: Iterable[Any]) -> tuple[FieldName, ...]:
    names: list[FieldName] = []
    for value in values:
        if value in FIELD_NAMES and value not in names:
            names.append(value)
    return tuple(names)


def load_services(configuration: Mapping[str, Any]) -> tuple[ServiceDefinition, ...]:
    """Build service definitions from a validated configuration document."""

    services: list[ServiceDefinition] = []
    raw_services: Mapping[str, Any] = configuration.get("services", {})
    for service_id in sorted(raw_services):
        entry = raw_services[service_id]
        services.append(
            ServiceDefinition(
                id=service_id,
                display_name=str(entry["display_name"]),
                aliases=tuple(str(alias) for alias in entry.get("aliases", ())),
            )
        )
    return tuple(services)


def load_risk_rules(configuration: Mapping[str, Any]) -> tuple[RiskRule, ...]:
    """Build risk rules from a validated configuration document.

    Configured order is preserved: chapter 04 rejects two rules sharing a risk
    type during configuration validation, so rule precedence is unnecessary and
    this order only affects result ordering, which is sorted before return.
    """

    rules: list[RiskRule] = []
    for entry in configuration.get("risk_rules", ()):
        match_terms: Mapping[str, Any] = entry.get("match", {})
        rules.append(
            RiskRule(
                id=str(entry["id"]),
                risk_type=str(entry["risk_type"]),
                priority=str(entry["priority"]),
                fields=_as_field_names(entry.get("fields", ())),
                any_terms=tuple(str(term) for term in match_terms.get("any", ())),
                all_terms=tuple(str(term) for term in match_terms.get("all", ())),
                none_terms=tuple(str(term) for term in match_terms.get("none", ())),
            )
        )
    return tuple(rules)


def _spans_by_field(
    announcement: Announcement,
    fields: Sequence[FieldName],
    phrases: Sequence[str],
) -> dict[FieldName, dict[str, tuple[Span, ...]]]:
    """Locate every phrase within every configured field."""

    located: dict[FieldName, dict[str, tuple[Span, ...]]] = {}
    for name in fields:
        haystack = fold(announcement.field_value(name))
        if not haystack:
            continue
        for phrase in phrases:
            spans = found_spans(haystack, fold(phrase))
            if spans:
                located.setdefault(name, {})[phrase] = spans
    return located


def _matched_phrases(located: Mapping[FieldName, Mapping[str, tuple[Span, ...]]]) -> set[str]:
    matched: set[str] = set()
    for phrases in located.values():
        matched.update(phrases)
    return matched


def _has_non_overlapping_pair(
    service_spans: Mapping[FieldName, Mapping[str, tuple[Span, ...]]],
    risk_spans: Mapping[FieldName, Mapping[str, tuple[Span, ...]]],
) -> bool:
    """Require service and risk evidence that do not occupy the same text.

    Spans in different fields never overlap. Within one field, an alias and a
    term covering the same characters are one piece of evidence read twice, so
    that pairing alone cannot produce a match.
    """

    for service_field, service_phrases in service_spans.items():
        for risk_field, risk_phrases in risk_spans.items():
            if service_field != risk_field:
                return True
            for service_field_spans in service_phrases.values():
                for risk_field_spans in risk_phrases.values():
                    for service_span in service_field_spans:
                        for risk_span in risk_field_spans:
                            if not service_span.overlaps(risk_span):
                                return True
    return False


def _evidence_fields(
    fields: Sequence[FieldName],
    *located: Mapping[FieldName, Mapping[str, tuple[Span, ...]]],
) -> tuple[FieldName, ...]:
    """Return contributing fields in configured order."""

    contributing = {name for entry in located for name in entry}
    return tuple(name for name in fields if name in contributing)


def match_announcement(
    announcement: Announcement,
    services: Sequence[ServiceDefinition],
    rules: Sequence[RiskRule],
) -> tuple[MatchResult, ...]:
    """Return every service and risk match, in deterministic order."""

    results: list[MatchResult] = []
    for rule in rules:
        if not rule.fields:
            continue

        excluded = _spans_by_field(announcement, rule.fields, rule.none_terms)
        if excluded:
            continue

        positive = _spans_by_field(announcement, rule.fields, rule.any_terms)
        required = _spans_by_field(announcement, rule.fields, rule.all_terms)

        matched_any = _matched_phrases(positive)
        matched_all = _matched_phrases(required)
        if rule.any_terms and not matched_any:
            continue
        if rule.all_terms and matched_all != set(rule.all_terms):
            continue

        # `all` terms are positive risk evidence too. A rule with only `all`
        # terms still needs a span to prove non-overlap against.
        risk_spans: dict[FieldName, dict[str, tuple[Span, ...]]] = {}
        for source in (positive, required):
            for name, phrases in source.items():
                risk_spans.setdefault(name, {}).update(phrases)
        if not risk_spans:
            continue

        for service in services:
            service_spans = _spans_by_field(announcement, rule.fields, service.aliases)
            if not service_spans:
                continue
            if not _has_non_overlapping_pair(service_spans, risk_spans):
                continue

            results.append(
                MatchResult(
                    service_id=service.id,
                    service_display_name=service.display_name,
                    rule_id=rule.id,
                    risk_type=rule.risk_type,
                    priority=rule.priority,
                    matched_aliases=tuple(sorted(_matched_phrases(service_spans), key=evidence_order)),
                    matched_terms=tuple(sorted(matched_any | matched_all, key=evidence_order)),
                    matched_fields=_evidence_fields(rule.fields, service_spans, risk_spans),
                )
            )

    results.sort(key=lambda result: (result.service_id, result.risk_type, result.rule_id))
    return tuple(results)
