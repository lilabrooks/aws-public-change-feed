"""Shared environment and invocation-value contracts for feed runtimes."""

from __future__ import annotations

import json
import os
import re

_SAFE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
_INVOCATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}")


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value:
        raise ValueError(f"missing required environment variable {name}")
    return value


def positive_environment_integer(name: str) -> int:
    raw = required_environment(name)
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer") from None
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def zero_redirect_policy() -> int:
    raw = required_environment("MAX_FEED_REDIRECTS")
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("MAX_FEED_REDIRECTS must be zero") from None
    if value != 0:
        raise ValueError("MAX_FEED_REDIRECTS must be zero")
    return value


def approved_hosts_from_environment() -> tuple[str, ...]:
    raw = required_environment("APPROVED_FEED_HOSTS_JSON")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("APPROVED_FEED_HOSTS_JSON must be a JSON array") from None
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("APPROVED_FEED_HOSTS_JSON must be a non-empty JSON array")
    hosts = tuple(value.casefold() for value in parsed if isinstance(value, str))
    if len(hosts) != len(parsed) or len(set(hosts)) != len(hosts):
        raise ValueError("APPROVED_FEED_HOSTS_JSON must contain unique strings")
    if any(_SAFE_VALUE.fullmatch(host) is None for host in hosts):
        raise ValueError("APPROVED_FEED_HOSTS_JSON contains an unsafe host")
    return hosts


def valid_invocation_id(value: object) -> bool:
    return isinstance(value, str) and _INVOCATION_ID.fullmatch(value) is not None
