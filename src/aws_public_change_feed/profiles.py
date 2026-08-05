"""Environment and route mapping for a matched service.

Chapter 04 "Environment and route mapping" defines the whole of this module:
select enabled environment policies whose profile contains the service, resolve
each environment through the exact inventory release, group environment IDs by
route, sort them, and create one candidate per route.

`scripts/validate_config.py` imports `route_audiences` rather than carrying a
second copy of the rule, for the reason `identity.py` gives about framing: a
validator that reimplements the mapping can agree with a fixture while
disagreeing with the runtime, and the fixture would still pass.

Customer labels, account IDs, and Regions are display context the worker loads
from the release. They are deliberately absent here, because ADR-002 keeps them
out of candidate identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .identity import audience_fingerprint

__all__ = [
    "RouteAudience",
    "route_audiences",
]


@dataclass(frozen=True, slots=True)
class RouteAudience:
    """The environments on one route that a matched service reaches.

    `environment_ids` and `profile_ids` are sorted by `route_audiences`, so the
    fingerprint and the candidate's explainability do not depend on inventory
    order.
    """

    route_id: str
    environment_ids: tuple[str, ...]
    profile_ids: tuple[str, ...]

    @property
    def audience_fingerprint(self) -> str:
        return audience_fingerprint(self.environment_ids)


def route_audiences(
    configuration: Mapping[str, Any],
    inventory: Mapping[str, Any],
    service_id: str,
) -> tuple[RouteAudience, ...]:
    """Group the environments that watch `service_id` by route.

    Returns one entry per route with at least one matching environment, ordered
    by route ID. A route whose environments do not watch the service yields no
    entry, and therefore no candidate.

    Sorting happens here rather than in the caller. Two callers that disagreed
    about ordering would produce two different `audience_fingerprint` values for
    the same audience, and the mismatch would surface as a duplicate candidate
    rather than as an error.
    """

    policies = configuration["environment_policies"]
    profiles = configuration["service_profiles"]
    routes = inventory["slack"]["routes"]

    environments_by_route: dict[str, list[str]] = {}
    profiles_by_route: dict[str, set[str]] = {}

    for environment in inventory["environments"]:
        environment_id = environment["id"]
        policy = policies.get(environment_id)
        # Chapter 04: disabled or unconfigured environments never appear. A
        # valid release cannot reach the `None` branch, because the validator
        # requires policies and inventory environments to name the same set.
        if policy is None or policy["feed_monitoring"] != "enabled":
            continue

        profile_id = policy["profile"]
        profile = profiles.get(profile_id)
        if profile is None:
            raise ValueError(f"environment policy {environment_id} names unknown service profile {profile_id}")
        if service_id not in profile["service_ids"]:
            continue

        route_id = environment["route_id"]
        if route_id not in routes:
            raise ValueError(f"environment {environment_id} names unknown route {route_id}")

        environments_by_route.setdefault(route_id, []).append(environment_id)
        profiles_by_route.setdefault(route_id, set()).add(profile_id)

    return tuple(
        RouteAudience(
            route_id=route_id,
            environment_ids=tuple(sorted(environments_by_route[route_id])),
            profile_ids=tuple(sorted(profiles_by_route[route_id])),
        )
        for route_id in sorted(environments_by_route)
    )
