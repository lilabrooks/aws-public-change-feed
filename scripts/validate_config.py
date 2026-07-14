#!/usr/bin/env python3

import argparse
import hashlib
import ipaddress
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit, urlunsplit

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

MINIMUM_PYTHON = (3, 12)
GENERIC_ALIASES = {"cluster", "engine version", "runtime", "version"}
TRACKING_QUERY_KEYS = {
    "sc_channel",
    "trk",
    "trkcampaign",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}
RUNTIME_FORBIDDEN_KEYS = {
    "account_id",
    "approved_webhook_hosts",
    "bot_token_secret_id",
    "channel_id",
    "credential_secret_id",
    "destination_key",
    "route_id",
    "secret_store",
    "workspace_id",
}
INVENTORY_ENVIRONMENT_KEYS = {"id", "customer", "account_id", "regions", "route_id"}


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping)


def construct_unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_yaml(path: Path):
    with path.open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=UniqueKeyLoader)


def load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=construct_unique_json_object)


def require_supported_python(version_info=sys.version_info):
    current = tuple(version_info[:2])
    if current < MINIMUM_PYTHON:
        required = ".".join(str(part) for part in MINIMUM_PYTHON)
        actual = ".".join(str(part) for part in current)
        raise ValueError(f"Python {required} or newer is required; found {actual}")


def schema_registry(schema_directory: Path) -> Registry:
    registry = Registry()
    for path in sorted(schema_directory.glob("*.schema.json")):
        schema = load_json(path)
        schema_id = schema.get("$id")
        if schema_id:
            registry = registry.with_resource(schema_id, Resource.from_contents(schema))
    return registry


def validate_schema(schema_path: Path, document_path: Path, document):
    schema = load_json(schema_path)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
        registry=schema_registry(schema_path.parent),
    )
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        details = []
        for error in errors:
            location = ".".join(str(part) for part in error.absolute_path) or "<root>"
            details.append(f"{document_path}:{location}: {error.message}")
        raise ValueError("\n".join(details))


def normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.casefold().split())


def digest_parts(*values: str) -> str:
    if any("\0" in value for value in values):
        raise ValueError("null-framed identity fields cannot contain null characters")
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def queue_dispatch_id(request_id: str, generation: int) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", request_id):
        raise ValueError("queue dispatch request_id must be a lowercase SHA-256 digest")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("queue dispatch generation must be a positive integer")
    return digest_parts("queue-dispatch:v1", request_id, str(generation))


def serialized_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def canonical_public_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url)
    host = (parsed.hostname or "").casefold()
    port = parsed.port
    netloc = host if port in (None, 443) else f"{host}:{port}"
    path = parsed.path or "/"
    query_parts = []
    for part in parsed.query.split("&") if parsed.query else []:
        encoded_key = part.partition("=")[0]
        if unquote_plus(encoded_key).casefold() not in TRACKING_QUERY_KEYS:
            query_parts.append(part)
    query = "&".join(query_parts)
    return urlunsplit((parsed.scheme.casefold(), netloc, path, query, ""))


def parsed_timestamp(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    return datetime.fromisoformat(normalized)


def walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_keys(child)


def validate_host(host: str, label: str):
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError(f"{label} cannot use an IP literal: {host}")
    labels = host.split(".")
    if not labels or any(
        not value
        or len(value) > 63
        or not value[0].isalnum()
        or not value[-1].isalnum()
        or any(not character.isalnum() and character != "-" for character in value)
        for value in labels
    ):
        raise ValueError(f"{label} is not a valid DNS hostname: {host}")


def validate_public_url(raw_url: str, allowed_hosts: set[str], label: str):
    parsed = urlsplit(raw_url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError(f"{label} must use unauthenticated HTTPS: {raw_url}")
    if parsed.port not in (None, 443):
        raise ValueError(f"{label} must use port 443: {raw_url}")
    if parsed.fragment:
        raise ValueError(f"{label} cannot contain a fragment: {raw_url}")
    host = (parsed.hostname or "").casefold()
    if host not in allowed_hosts:
        raise ValueError(f"{label} host is not allowed: {host}")
    validate_host(host, label)


def validate_feed_url(raw_url: str, allowed_hosts: set[str]):
    validate_public_url(raw_url, allowed_hosts, "feed URL")


def phrase_spans(text: str, phrase: str) -> list[tuple[int, int]]:
    normalized_content = normalized_text(text)
    normalized_phrase = normalized_text(phrase)
    pattern = re.compile(rf"(?<!\w){re.escape(normalized_phrase)}(?!\w)")
    return [(match.start(), match.end()) for match in pattern.finditer(normalized_content)]


def validate_slack(deployment):
    slack = deployment["slack"]
    routes = slack["routes"]
    if slack["default_route_id"] not in routes:
        raise ValueError("slack.default_route_id does not exist in slack.routes")

    destination_owners: dict[str, str] = {}
    for route_id, route in routes.items():
        destination_key = route["destination_key"]
        owner = destination_owners.get(destination_key)
        if owner:
            raise ValueError(f"Slack destination is shared by routes {owner} and {route_id}")
        destination_owners[destination_key] = route_id

    if slack["delivery_mode"] == "incoming_webhook":
        secret_owners: dict[str, str] = {}
        for host in slack["approved_webhook_hosts"]:
            validate_host(host, "Slack webhook host")
        for route_id, route in routes.items():
            if not route.get("credential_secret_id"):
                raise ValueError(f"incoming-webhook route needs credential_secret_id: {route_id}")
            if route.get("channel_id"):
                raise ValueError(f"incoming-webhook route cannot contain channel_id: {route_id}")
            secret_id = route["credential_secret_id"]
            owner = secret_owners.get(secret_id)
            if owner:
                raise ValueError(f"incoming-webhook secret is shared by routes {owner} and {route_id}")
            secret_owners[secret_id] = route_id
    else:
        if not slack.get("bot_token_secret_id") or not slack.get("workspace_id"):
            raise ValueError("bot-token delivery needs workspace_id and bot_token_secret_id")
        for route_id, route in routes.items():
            channel_id = route.get("channel_id")
            if not channel_id:
                raise ValueError(f"bot-token route needs channel_id: {route_id}")
            if route.get("credential_secret_id"):
                raise ValueError(f"bot-token route cannot contain credential_secret_id: {route_id}")
            expected_destination = f"{slack['workspace_id']}-{channel_id}".casefold()
            if route["destination_key"] != expected_destination:
                raise ValueError(f"bot-token destination_key differs from workspace and channel for {route_id}")

    rate = slack["rate_control"]
    if rate["queue_max_receive_count"] <= rate["max_network_attempts"] + 5:
        raise ValueError("queue_max_receive_count must leave room for rate deferrals and network attempts")


def validate_semantics(deployment, config, inventory):
    release_prefix = deployment["release_prefix"].rstrip("/")
    if deployment["active_versions_object_key"].startswith(f"{release_prefix}/"):
        raise ValueError("active-versions object key cannot be inside the immutable release prefix")
    if deployment["config_filename"] == deployment["inventory_filename"]:
        raise ValueError("config and inventory filenames must be distinct")

    validate_slack(deployment)
    routes = deployment["slack"]["routes"]
    environments = deployment["environments"]
    environment_ids = [environment["id"] for environment in environments]
    if len(environment_ids) != len(set(environment_ids)):
        raise ValueError("deployment environment IDs must be unique")
    for environment in environments:
        if environment["route_id"] not in routes:
            raise ValueError(f"unknown route for environment {environment['id']}")

    envelope = deployment["scale_envelope"]
    counts = {
        "max_accounts": len({environment["account_id"] for environment in environments}),
        "max_environments": len(environments),
        "max_feeds": len(config["feeds"]),
        "max_slack_routes": len(routes),
        "max_service_definitions": len(config["services"]),
        "max_service_profiles": len(config["service_profiles"]),
        "max_risk_rules": len(config["risk_rules"]),
    }
    for field, actual in counts.items():
        if actual > envelope[field]:
            raise ValueError(f"{field} exceeded: {actual} > {envelope[field]}")
    destination_capacity = envelope["max_delivery_requests_per_destination_per_hour"] * len(routes)
    if envelope["max_delivery_requests_per_hour"] > destination_capacity:
        raise ValueError("global delivery envelope exceeds the configured destination capacity")
    interval = deployment["slack"]["rate_control"]["per_destination_min_interval_seconds"]
    interval_capacity = 3600 // interval
    if envelope["max_delivery_requests_per_destination_per_hour"] > interval_capacity:
        raise ValueError("per-destination delivery envelope exceeds configured Slack pacing")
    request_timeout = deployment["slack"]["rate_control"]["slack_request_timeout_seconds"]
    worker_capacity = deployment["slack"]["rate_control"]["worker_reserved_concurrency"] * (3600 // request_timeout)
    if envelope["max_delivery_requests_per_hour"] > worker_capacity:
        raise ValueError("global delivery envelope exceeds the timeout-derived worker upper bound")

    allowed_hosts = {host.casefold() for host in deployment["feed_fetch_policy"]["allowed_feed_hosts"]}
    for host in allowed_hosts:
        validate_host(host, "allowed feed host")
    feed_names = [feed["name"] for feed in config["feeds"]]
    feed_urls = [canonical_public_url(feed["url"]) for feed in config["feeds"]]
    if len(feed_names) != len(set(feed_names)):
        raise ValueError("feed names must be unique")
    if len(feed_urls) != len(set(feed_urls)):
        raise ValueError("feed URLs must be unique after normalization")
    for feed in config["feeds"]:
        validate_feed_url(feed["url"], allowed_hosts)
    configured_feed_hosts = {(urlsplit(feed["url"]).hostname or "").casefold() for feed in config["feeds"]}
    unused_allowed_hosts = sorted(allowed_hosts - configured_feed_hosts)
    if unused_allowed_hosts:
        raise ValueError(f"allowed feed hosts are unused: {unused_allowed_hosts}")

    inventory_environment_ids = {environment["id"] for environment in inventory["environments"]}
    policy_environment_ids = set(config["environment_policies"])
    if policy_environment_ids != inventory_environment_ids:
        missing = sorted(inventory_environment_ids - policy_environment_ids)
        unknown = sorted(policy_environment_ids - inventory_environment_ids)
        raise ValueError(f"environment policies must cover inventory exactly; missing={missing}, unknown={unknown}")

    services = config["services"]
    referenced_services = set()
    for profile_id, profile in config["service_profiles"].items():
        for service_id in profile["service_ids"]:
            if service_id not in services:
                raise ValueError(f"profile {profile_id} references unknown service: {service_id}")
            referenced_services.add(service_id)
    unused_services = sorted(set(services) - referenced_services)
    if unused_services:
        raise ValueError(f"service definitions are unused: {unused_services}")

    for environment_id, policy in config["environment_policies"].items():
        if policy["feed_monitoring"] == "enabled" and policy["profile"] not in config["service_profiles"]:
            raise ValueError(f"environment {environment_id} references unknown profile: {policy['profile']}")

    alias_owners: dict[str, str] = {}
    for service_id, service in services.items():
        for alias in service["aliases"]:
            normalized = normalized_text(alias)
            if normalized in GENERIC_ALIASES:
                raise ValueError(f"generic alias for service {service_id}: {alias}")
            owner = alias_owners.get(normalized)
            if owner:
                raise ValueError(f"alias collision: {alias} is repeated for {owner} and {service_id}")
            alias_owners[normalized] = service_id

    risk_ids = [rule["id"] for rule in config["risk_rules"]]
    risk_types = [rule["risk_type"] for rule in config["risk_rules"]]
    if len(risk_ids) != len(set(risk_ids)):
        raise ValueError("risk rule IDs must be unique")
    if len(risk_types) != len(set(risk_types)):
        raise ValueError("risk types must be unique")
    for rule in config["risk_rules"]:
        match = rule["match"]
        normalized_sets = {name: {normalized_text(term) for term in match[name]} for name in ("any", "all", "none")}
        for name, terms in match.items():
            if len(normalized_sets[name]) != len(terms):
                raise ValueError(f"risk rule repeats normalized terms in {name}: {rule['id']}")
        if not normalized_sets["any"] and not normalized_sets["all"]:
            raise ValueError(f"risk rule needs a positive term: {rule['id']}")
        if normalized_sets["any"].intersection(normalized_sets["all"]):
            raise ValueError(f"risk rule repeats positive terms across any and all: {rule['id']}")
        positives = normalized_sets["any"].union(normalized_sets["all"])
        if positives.intersection(normalized_sets["none"]):
            raise ValueError(f"risk rule has a term in positive and excluded sets: {rule['id']}")
        alias_conflicts = sorted(positives.intersection(alias_owners))
        if alias_conflicts:
            raise ValueError(f"risk terms cannot equal service aliases: {alias_conflicts}")

    forbidden = RUNTIME_FORBIDDEN_KEYS.intersection(walk_keys(config))
    if forbidden:
        raise ValueError(f"runtime config contains deployment-owned keys: {sorted(forbidden)}")

    retention = config["state_retention"]
    delivery_days = retention["delivery_state_ttl_days"]
    if retention["feed_state_ttl_days"] < delivery_days:
        raise ValueError("feed state retention cannot be shorter than delivery state retention")
    if retention["announcement_state_ttl_days"] < delivery_days:
        raise ValueError("announcement state retention cannot be shorter than delivery state retention")
    if deployment["s3_lifecycle"]["retired_release_retention_days"] < delivery_days:
        raise ValueError("retired release retention cannot be shorter than delivery state retention")

    if inventory["deployment_id"] != deployment["deployment_id"]:
        raise ValueError("inventory deployment_id differs from deployment.yaml")
    if inventory["deployment_region"] != deployment["deployment_region"]:
        raise ValueError("inventory deployment_region differs from deployment.yaml")
    if inventory["slack"] != deployment["slack"]:
        raise ValueError("inventory Slack projection differs from deployment.yaml")
    projected_environments = [
        {key: environment[key] for key in INVENTORY_ENVIRONMENT_KEYS} for environment in environments
    ]
    if sorted(inventory["environments"], key=lambda item: item["id"]) != sorted(
        projected_environments, key=lambda item: item["id"]
    ):
        raise ValueError("inventory environment projection differs from deployment.yaml")


def validate_manifest(root: Path, deployment, manifest):
    local_paths = {
        "config": root / "examples/config.yaml",
        "inventory": root / "examples/inventory.json",
    }
    release_root = f"{deployment['release_prefix'].rstrip('/')}/{manifest['release_id']}"
    expected_keys = {
        "config": f"{release_root}/{deployment['config_filename']}",
        "inventory": f"{release_root}/{deployment['inventory_filename']}",
    }
    release_parts = ("release:v1", manifest["config"]["sha256"], manifest["inventory"]["sha256"])
    expected_release_id = digest_parts(*release_parts)
    if manifest["release_id"] != expected_release_id:
        raise ValueError("active-versions release_id differs from artifact hashes")
    for name, path in local_paths.items():
        if manifest[name]["key"] != expected_keys[name]:
            raise ValueError(f"active-versions {name} key differs from deployment.yaml")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if manifest[name]["sha256"] != digest:
            raise ValueError(f"active-versions hash differs from {path}")
    expected_schema_versions = {
        "config": load_yaml(local_paths["config"])["version"],
        "inventory": load_json(local_paths["inventory"])["schema_version"],
    }
    for name, expected_version in expected_schema_versions.items():
        if manifest[name]["schema_version"] != expected_version:
            raise ValueError(f"active-versions {name} schema_version differs from its artifact")
    inventory_generated_at = load_json(local_paths["inventory"])["generated_at"]
    if parsed_timestamp(manifest["promoted_at"]) < parsed_timestamp(inventory_generated_at):
        raise ValueError("active-versions promotion predates inventory generation")


def expected_release(manifest, application_version: str):
    return {
        "release_id": manifest["release_id"],
        "config": manifest["config"],
        "inventory": manifest["inventory"],
        "application_version": application_version,
    }


def validate_candidate_semantics(config, inventory, manifest, candidate):
    announcement = candidate["announcement"]
    feed_hosts = {(urlsplit(feed["url"]).hostname or "").casefold() for feed in config["feeds"]}
    validate_public_url(announcement["url"], feed_hosts, "announcement URL")
    expected_announcement_id = hashlib.sha256(canonical_public_url(announcement["url"]).encode()).hexdigest()
    if announcement["announcement_id"] != expected_announcement_id:
        raise ValueError("announcement_id differs from the canonical announcement URL")
    expected_fingerprint = digest_parts(
        "announcement-content:v1",
        normalized_text(announcement["title"]),
        normalized_text(announcement["summary"]),
    )
    if announcement["content_fingerprint"] != expected_fingerprint:
        raise ValueError("content_fingerprint differs from normalized announcement content")
    expected_revision_id = digest_parts(
        "announcement-revision:v1",
        announcement["announcement_id"],
        announcement["content_fingerprint"],
    )
    if announcement["revision_id"] != expected_revision_id:
        raise ValueError("revision_id differs from announcement identity and content")
    expected_audience_fingerprint = digest_parts("candidate-audience:v1", *sorted(candidate["environment_ids"]))
    if candidate["audience_fingerprint"] != expected_audience_fingerprint:
        raise ValueError("audience_fingerprint differs from sorted candidate environment IDs")
    expected_candidate_id = digest_parts(
        "candidate:v3",
        announcement["revision_id"],
        candidate["service"]["id"],
        candidate["risk"]["risk_type"],
        candidate["route_id"],
        candidate["audience_fingerprint"],
    )
    if candidate["candidate_id"] != expected_candidate_id:
        raise ValueError("candidate_id differs from its canonical identity fields")
    if parsed_timestamp(candidate["created_at"]) < parsed_timestamp(announcement["observed_at"]):
        raise ValueError("candidate creation predates announcement observation")
    if parsed_timestamp(candidate["created_at"]) < parsed_timestamp(manifest["promoted_at"]):
        raise ValueError("candidate creation predates its release promotion")

    application_version = candidate["release"]["application_version"]
    if candidate["release"] != expected_release(manifest, application_version):
        raise ValueError("candidate release differs from active manifest")

    service_id = candidate["service"]["id"]
    service = config["services"].get(service_id)
    if not service:
        raise ValueError(f"candidate references unknown service: {service_id}")
    if candidate["service"]["display_name"] != service["display_name"]:
        raise ValueError("candidate service display name differs from config")
    if candidate["recommended_action"] != service["recommended_action"]:
        raise ValueError("candidate recommended action differs from config")

    rules = {rule["id"]: rule for rule in config["risk_rules"]}
    rule = rules.get(candidate["risk"]["rule_id"])
    if not rule:
        raise ValueError("candidate references unknown risk rule")
    for field in ("risk_type", "priority"):
        if candidate["risk"][field] != rule[field]:
            raise ValueError(f"candidate risk {field} differs from config")

    fields = {field: announcement[field] for field in rule["fields"]}
    matched_alias_values = sorted(
        (alias for alias in service["aliases"] if any(phrase_spans(text, alias) for text in fields.values())),
        key=normalized_text,
    )
    if candidate["explainability"]["matched_aliases"] != matched_alias_values:
        raise ValueError("candidate matched aliases differ from announcement service evidence")

    term_matches = {
        group: {
            term: {field: phrase_spans(text, term) for field, text in fields.items()} for term in rule["match"][group]
        }
        for group in ("any", "all", "none")
    }
    present_any = [term for term, locations in term_matches["any"].items() if any(locations.values())]
    present_all = [term for term, locations in term_matches["all"].items() if any(locations.values())]
    present_none = [term for term, locations in term_matches["none"].items() if any(locations.values())]
    if (rule["match"]["any"] and not present_any) or len(present_all) != len(rule["match"]["all"]) or present_none:
        raise ValueError("candidate announcement does not satisfy its risk rule")
    matched_term_values = sorted(present_any + present_all, key=normalized_text)
    if candidate["explainability"]["matched_terms"] != matched_term_values:
        raise ValueError("candidate matched terms differ from announcement risk evidence")

    evidence_fields = []
    distinct_evidence = False
    for field in rule["fields"]:
        alias_spans = [span for alias in matched_alias_values for span in phrase_spans(fields[field], alias)]
        risk_spans = [span for term in matched_term_values for span in phrase_spans(fields[field], term)]
        if alias_spans or risk_spans:
            evidence_fields.append(field)
        if any(
            alias_end <= risk_start or risk_end <= alias_start
            for alias_start, alias_end in alias_spans
            for risk_start, risk_end in risk_spans
        ):
            distinct_evidence = True
    if not distinct_evidence:
        alias_fields = {
            field
            for field in rule["fields"]
            if any(phrase_spans(fields[field], value) for value in matched_alias_values)
        }
        risk_fields = {
            field
            for field in rule["fields"]
            if any(phrase_spans(fields[field], value) for value in matched_term_values)
        }
        distinct_evidence = bool(alias_fields and risk_fields and alias_fields != risk_fields)
    if not distinct_evidence:
        raise ValueError("candidate service and risk evidence must be distinct")
    if candidate["explainability"]["matched_fields"] != evidence_fields:
        raise ValueError("candidate matched fields differ from announcement evidence")

    profiles = config["service_profiles"]
    inventory_environments = {environment["id"]: environment for environment in inventory["environments"]}
    if candidate["route_id"] not in inventory["slack"]["routes"]:
        raise ValueError("candidate references unknown route")
    expected_environment_ids = []
    expected_profile_ids = set()
    for environment_id, environment in inventory_environments.items():
        policy = config["environment_policies"][environment_id]
        if policy["feed_monitoring"] != "enabled" or environment["route_id"] != candidate["route_id"]:
            continue
        profile_id = policy["profile"]
        if service_id in profiles[profile_id]["service_ids"]:
            expected_environment_ids.append(environment_id)
            expected_profile_ids.add(profile_id)
    if candidate["environment_ids"] != sorted(expected_environment_ids):
        raise ValueError("candidate environment IDs differ from the complete route and profile mapping")
    if candidate["explainability"]["matched_profile_ids"] != sorted(expected_profile_ids):
        raise ValueError("candidate matched profile IDs differ from the complete environment mapping")

    feed_urls = {feed["name"]: canonical_public_url(feed["url"]) for feed in config["feeds"]}
    provenance_order = [(item["feed_name"], item["source_item_id"]) for item in announcement["provenance"]]
    if provenance_order != sorted(provenance_order):
        raise ValueError("announcement provenance must use stable lexical order")
    for item in announcement["provenance"]:
        expected_url = feed_urls.get(item["feed_name"])
        if expected_url is None:
            raise ValueError(f"announcement provenance names an unknown feed: {item['feed_name']}")
        if canonical_public_url(item["feed_url"]) != expected_url:
            raise ValueError(f"announcement provenance URL differs from config: {item['feed_name']}")
        if item.get("source_item_url"):
            validate_public_url(item["source_item_url"], feed_hosts, "source item URL")
            if canonical_public_url(item["source_item_url"]) != canonical_public_url(announcement["url"]):
                raise ValueError("announcement provenance item URL differs from the canonical announcement URL")

    policy = config["message_policy"]
    if len(announcement["title"]) > policy["max_title_characters"]:
        raise ValueError("candidate title exceeds configured rendering limit")
    if len(announcement["summary"]) > policy["max_summary_characters"]:
        raise ValueError("candidate summary exceeds configured rendering limit")
    if serialized_size(candidate) > policy["max_candidate_bytes"]:
        raise ValueError(f"alert candidate exceeds {policy['max_candidate_bytes']} UTF-8 JSON bytes")


def validate_event_contract_semantics(config, inventory, manifest, candidate, delivery_request):
    validate_candidate_semantics(config, inventory, manifest, candidate)
    if delivery_request["candidate"] != candidate:
        raise ValueError("delivery request embedded candidate differs from alert candidate")
    expected_request_id = digest_parts("delivery-request:v2", candidate["candidate_id"])
    if delivery_request["request_id"] != expected_request_id:
        raise ValueError("delivery request_id differs from its candidate")
    if parsed_timestamp(delivery_request["created_at"]) < parsed_timestamp(candidate["created_at"]):
        raise ValueError("delivery request creation predates its candidate")
    route = inventory["slack"]["routes"][candidate["route_id"]]
    if delivery_request["destination_key"] != route["destination_key"]:
        raise ValueError("delivery destination_key differs from candidate route")
    maximum_size = config["message_policy"]["max_delivery_request_bytes"]
    if serialized_size(delivery_request) > maximum_size:
        raise ValueError(f"delivery request exceeds {maximum_size} UTF-8 JSON bytes")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    return parser.parse_args()


def main():
    require_supported_python()
    root = parse_args().root.resolve()
    documents = {
        "deployment": load_yaml(root / "examples/deployment.yaml"),
        "config": load_yaml(root / "examples/config.yaml"),
        "inventory": load_json(root / "examples/inventory.json"),
        "active_versions": load_json(root / "examples/active-versions.json"),
        "alert_candidate": load_json(root / "examples/alert-candidate.json"),
        "delivery_request": load_json(root / "examples/delivery-request.json"),
    }
    schema_pairs = {
        "deployment": "deployment.schema.json",
        "config": "config.schema.json",
        "inventory": "inventory.schema.json",
        "active_versions": "active-versions.schema.json",
        "alert_candidate": "alert-candidate.schema.json",
        "delivery_request": "delivery-request.schema.json",
    }
    for name, schema_name in schema_pairs.items():
        validate_schema(
            root / "schemas" / schema_name,
            root
            / "examples"
            / ({"deployment": "deployment.yaml", "config": "config.yaml"}.get(name, f"{name.replace('_', '-')}.json")),
            documents[name],
        )
    validate_semantics(documents["deployment"], documents["config"], documents["inventory"])
    validate_manifest(root, documents["deployment"], documents["active_versions"])
    validate_event_contract_semantics(
        documents["config"],
        documents["inventory"],
        documents["active_versions"],
        documents["alert_candidate"],
        documents["delivery_request"],
    )
    print("public-feed configuration and event contracts passed schema and semantic validation")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, yaml.YAMLError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from None
