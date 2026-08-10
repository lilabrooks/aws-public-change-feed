"""Format checks that never depend on jsonschema's optional packages.

The owned ``uri`` format is intentionally limited to absolute hierarchical
URIs with an authority component. Every current URI-formatted field is a feed
or announcement URL; later semantic checks impose its HTTPS and host policy.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit

from jsonschema import FormatChecker

from .urls import validate_uri_spelling

__all__ = ["contract_format_checker"]

_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")


def _is_date_time(value: object) -> bool:
    if not isinstance(value, str) or _RFC3339.fullmatch(value) is None:
        return False
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _is_uri(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        validate_uri_spelling(value)
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return bool(_SCHEME.fullmatch(parsed.scheme) and parsed.netloc)


def contract_format_checker() -> FormatChecker:
    """Return the checker shared by source validation and installed runtime."""

    checker = FormatChecker()
    checker.checks("date-time")(_is_date_time)
    checker.checks("uri")(_is_uri)
    return checker
