"""Loading the labeled corpus and its approved thresholds.

Shape is enforced by ``schemas/corpus.schema.json`` and
``schemas/corpus-thresholds.schema.json``. This module assumes those checks
have run and converts validated documents into typed records.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .matching import Announcement

__all__ = [
    "CorpusItem",
    "ExpectedMatch",
    "Thresholds",
    "load_corpus",
    "load_thresholds",
]


@dataclass(frozen=True, slots=True)
class ExpectedMatch:
    service_id: str
    risk_type: str

    @property
    def pair(self) -> tuple[str, str]:
        return (self.service_id, self.risk_type)


@dataclass(frozen=True, slots=True)
class CorpusItem:
    id: str
    provenance: str
    category: str
    canonical_url: str
    title: str
    summary: str
    expected_matches: tuple[ExpectedMatch, ...]
    notes: str | None = None

    @property
    def announcement(self) -> Announcement:
        return Announcement(title=self.title, summary=self.summary)

    @property
    def expected_pairs(self) -> frozenset[tuple[str, str]]:
        return frozenset(expected.pair for expected in self.expected_matches)


@dataclass(frozen=True, slots=True)
class Threshold:
    min_precision: float
    min_recall: float


@dataclass(frozen=True, slots=True)
class Override:
    service_id: str
    risk_type: str
    min_precision: float
    min_recall: float
    reason: str

    @property
    def pair(self) -> tuple[str, str]:
        return (self.service_id, self.risk_type)


@dataclass(frozen=True, slots=True)
class Thresholds:
    global_threshold: Threshold
    overrides: tuple[Override, ...] = ()

    def for_pair(self, pair: tuple[str, str]) -> Threshold:
        for override in self.overrides:
            if override.pair == pair:
                return Threshold(override.min_precision, override.min_recall)
        return self.global_threshold

    def has_override(self, pair: tuple[str, str]) -> bool:
        """Whether this pair carries its own approved numbers and therefore gates."""

        return any(override.pair == pair for override in self.overrides)


def _read_json(path: Path) -> Mapping[str, Any]:
    with path.open(encoding="utf-8") as handle:
        document: Mapping[str, Any] = json.load(handle)
    return document


def load_corpus(path: Path) -> tuple[CorpusItem, ...]:
    """Read the corpus, sorted by item ID for deterministic reporting."""

    document = _read_json(path)
    raw_items: Sequence[Mapping[str, Any]] = document["items"]
    items = [
        CorpusItem(
            id=str(entry["id"]),
            provenance=str(entry["provenance"]),
            category=str(entry["category"]),
            canonical_url=str(entry["canonical_url"]),
            title=str(entry["title"]),
            summary=str(entry.get("summary", "")),
            expected_matches=tuple(
                ExpectedMatch(str(expected["service_id"]), str(expected["risk_type"]))
                for expected in entry.get("expected_matches", ())
            ),
            notes=str(entry["notes"]) if "notes" in entry else None,
        )
        for entry in raw_items
    ]
    items.sort(key=lambda item: item.id)
    return tuple(items)


def load_thresholds(path: Path) -> Thresholds:
    document = _read_json(path)
    raw_global: Mapping[str, Any] = document["global"]
    overrides = tuple(
        Override(
            service_id=str(entry["service_id"]),
            risk_type=str(entry["risk_type"]),
            min_precision=float(entry["min_precision"]),
            min_recall=float(entry["min_recall"]),
            reason=str(entry["reason"]),
        )
        for entry in document.get("overrides", ())
    )
    return Thresholds(
        global_threshold=Threshold(
            min_precision=float(raw_global["min_precision"]),
            min_recall=float(raw_global["min_recall"]),
        ),
        overrides=overrides,
    )
