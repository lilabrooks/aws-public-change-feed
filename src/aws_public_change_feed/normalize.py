"""Text normalization and phrase-boundary search shared by matching and tests.

Chapter 04 requires service aliases and risk terms to be found "using
normalized phrase boundaries", and requires one normalization path for runtime
and test vectors. Every caller goes through this module so a corpus result and
a runtime result cannot diverge.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = ["Span", "fold", "found_spans", "normalize_text"]

_WHITESPACE = re.compile(r"\s+")

# A phrase boundary is any position not flanked by a word character. Hyphens
# are word-adjacent in AWS prose ("end-of-support"), so they are deliberately
# excluded from the word class: "end-of-support" and "end of support" are
# distinct configured terms rather than one fuzzy match.
_WORD = re.compile(r"\w")


@dataclass(frozen=True, slots=True)
class Span:
    """A half-open character range within a folded field value."""

    start: int
    end: int

    def overlaps(self, other: Span) -> bool:
        return self.start < other.end and other.start < self.end


def normalize_text(value: str) -> str:
    """Apply Unicode normalization and collapse whitespace, preserving case."""

    normalized = unicodedata.normalize("NFKC", value)
    return _WHITESPACE.sub(" ", normalized).strip()


def fold(value: str) -> str:
    """Normalize and case-fold a value for comparison."""

    return normalize_text(value).casefold()


def _is_word_character(value: str, index: int) -> bool:
    if index < 0 or index >= len(value):
        return False
    return _WORD.match(value[index]) is not None


def found_spans(haystack: str, needle: str) -> tuple[Span, ...]:
    """Return every phrase-boundary occurrence of ``needle`` within ``haystack``.

    Both arguments must already be folded. An empty needle never matches, so a
    blank configured alias cannot make every announcement match.
    """

    if not needle or not haystack:
        return ()

    spans: list[Span] = []
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        leading_is_word = _is_word_character(needle, 0)
        trailing_is_word = _is_word_character(needle, len(needle) - 1)
        boundary_before = not leading_is_word or not _is_word_character(haystack, start - 1)
        boundary_after = not trailing_is_word or not _is_word_character(haystack, end)
        if boundary_before and boundary_after:
            spans.append(Span(start, end))
        start = haystack.find(needle, start + 1)
    return tuple(spans)
