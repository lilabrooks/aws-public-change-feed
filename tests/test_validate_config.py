import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema.exceptions import FormatError, SchemaError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import validate_config as validator  # noqa: E402

from aws_public_change_feed import identity  # noqa: E402
from aws_public_change_feed.schema_formats import contract_format_checker  # noqa: E402

SCHEMA_FILES = {
    "deployment": "deployment.schema.json",
    "config": "config.schema.json",
    "inventory": "inventory.schema.json",
    "active_versions": "active-versions.schema.json",
    "alert_candidate": "alert-candidate.schema.json",
    "delivery_request": "delivery-request.schema.json",
}
DOCUMENT_FILES = {
    "deployment": "deployment.yaml",
    "config": "config.yaml",
    "inventory": "inventory.json",
    "active_versions": "active-versions.json",
    "alert_candidate": "alert-candidate.json",
    "delivery_request": "delivery-request.json",
}


def load_documents():
    return {
        "deployment": validator.load_yaml(ROOT / "examples/deployment.yaml"),
        "config": validator.load_yaml(ROOT / "examples/config.yaml"),
        "inventory": validator.load_json(ROOT / "examples/inventory.json"),
        "active_versions": validator.load_json(ROOT / "examples/active-versions.json"),
        "alert_candidate": validator.load_json(ROOT / "examples/alert-candidate.json"),
        "delivery_request": validator.load_json(ROOT / "examples/delivery-request.json"),
    }


def move_environment_to_new_route(documents):
    """Move the first environment onto a second, fully valid route.

    The candidate keeps naming its original route, so its committed environment
    list is no longer that route's complete audience. Derived from the shipped
    inventory rather than hand-built, so the case follows the fixture when the
    example release changes.
    """

    inventory = documents["inventory"]
    routes = inventory["slack"]["routes"]
    source_route_id = inventory["environments"][0]["route_id"]
    new_route = copy.deepcopy(routes[source_route_id])
    new_route["destination_key"] = "second-aws-change-alerts"
    if "credential_secret_id" in new_route:
        new_route["credential_secret_id"] = "aws-public-change-alerting/slack/second-alerts-webhook"
    routes["second-alerts"] = new_route
    inventory["environments"][0]["route_id"] = "second-alerts"


def sync_inventory(documents):
    deployment = documents["deployment"]
    inventory = documents["inventory"]
    inventory["deployment_id"] = deployment["deployment_id"]
    inventory["deployment_region"] = deployment["deployment_region"]
    inventory["slack"] = copy.deepcopy(deployment["slack"])
    inventory["environments"] = [
        {key: copy.deepcopy(environment[key]) for key in validator.INVENTORY_ENVIRONMENT_KEYS}
        for environment in deployment["environments"]
    ]


def sync_candidate_identity(candidate):
    announcement = candidate["announcement"]
    announcement["announcement_id"] = hashlib.sha256(
        validator.canonical_public_url(announcement["url"]).encode()
    ).hexdigest()
    announcement["content_fingerprint"] = identity.digest_parts(
        "announcement-content:v1",
        identity.identity_text(announcement["title"]),
        identity.identity_text(announcement["summary"]),
    )
    announcement["revision_id"] = identity.digest_parts(
        "announcement-revision:v1",
        announcement["announcement_id"],
        announcement["content_fingerprint"],
    )
    candidate["audience_fingerprint"] = identity.digest_parts(
        "candidate-audience:v1", *sorted(candidate["environment_ids"])
    )
    candidate["candidate_id"] = identity.digest_parts(
        "candidate:v3",
        announcement["revision_id"],
        candidate["service"]["id"],
        candidate["risk"]["risk_type"],
        candidate["route_id"],
        candidate["audience_fingerprint"],
    )


class ConfigurationValidatorTests(unittest.TestCase):
    def setUp(self):
        self.documents = load_documents()

    def validate_schema(self, name, document=None):
        validator.validate_schema(
            ROOT / "schemas" / SCHEMA_FILES[name],
            ROOT / "examples" / DOCUMENT_FILES[name],
            self.documents[name] if document is None else document,
        )

    def validate_contract(self, documents=None):
        documents = self.documents if documents is None else documents
        for name in ("deployment", "config", "inventory"):
            self.validate_schema(name, documents[name])
        validator.validate_semantics(
            documents["deployment"],
            documents["config"],
            documents["inventory"],
        )

    def validate_candidate(self, candidate=None, documents=None):
        documents = self.documents if documents is None else documents
        candidate = documents["alert_candidate"] if candidate is None else candidate
        validator.validate_candidate_semantics(
            documents["config"],
            documents["inventory"],
            documents["active_versions"],
            candidate,
        )

    def test_committed_examples_pass_full_validation(self):
        self.validate_contract()
        for name in ("active_versions", "alert_candidate", "delivery_request"):
            self.validate_schema(name)
        validator.validate_manifest(ROOT, self.documents["deployment"], self.documents["active_versions"])
        validator.validate_event_contract_semantics(
            self.documents["config"],
            self.documents["inventory"],
            self.documents["active_versions"],
            self.documents["alert_candidate"],
            self.documents["delivery_request"],
        )

    def test_python_312_is_the_minimum(self):
        with self.assertRaisesRegex(ValueError, "Python 3.12 or newer"):
            validator.require_supported_python((3, 11, 9))
        validator.require_supported_python((3, 12, 0))

    def test_duplicate_yaml_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.yaml"
            path.write_text("version: 2\nversion: 3\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate YAML key: version"):
                validator.load_yaml(path)

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"version": 2, "version": 3}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key: version"):
                validator.load_json(path)

    def test_invalid_json_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            schema_path = Path(directory) / "invalid.schema.json"
            schema_path.write_text(
                '{"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "invalid"}\n',
                encoding="utf-8",
            )
            with self.assertRaises(SchemaError):
                validator.validate_schema(schema_path, Path("example.json"), {})

    def test_contract_format_checks_are_registered_without_optional_packages(self):
        checker = contract_format_checker()

        for format_name, invalid in (("date-time", "tomorrow"), ("uri", "https://aws.amazon.com/a b")):
            with self.subTest(format_name=format_name):
                with self.assertRaises(FormatError):
                    checker.check(invalid, format_name)

    def test_owned_schemas_reject_malformed_dates_and_uris(self):
        cases = (
            ("active_versions", "promoted_at", "tomorrow", "date-time"),
            ("alert_candidate", "created_at", "2026-99-99T00:00:00Z", "date-time"),
            ("delivery_request", "created_at", "2026-07-13", "date-time"),
        )
        for name, field, value, format_name in cases:
            with self.subTest(document=name):
                document = copy.deepcopy(self.documents[name])
                document[field] = value
                with self.assertRaisesRegex(ValueError, format_name):
                    self.validate_schema(name, document)

        candidate = copy.deepcopy(self.documents["alert_candidate"])
        candidate["announcement"]["url"] = "https://aws.amazon.com/a b"
        with self.assertRaisesRegex(ValueError, "uri"):
            self.validate_schema("alert_candidate", candidate)

    def test_breaking_document_versions_are_rejected(self):
        cases = {
            "deployment": ("schema_version", 2),
            "config": ("version", 3),
            "inventory": ("schema_version", 2),
        }
        for name, (field, old_version) in cases.items():
            with self.subTest(name=name):
                document = copy.deepcopy(self.documents[name])
                document[field] = old_version
                expected = "4 was expected" if name == "config" else "3 was expected"
                with self.assertRaisesRegex(ValueError, expected):
                    self.validate_schema(name, document)

        active_versions = copy.deepcopy(self.documents["active_versions"])
        active_versions["schema_version"] = 1
        with self.assertRaisesRegex(ValueError, "2 was expected"):
            self.validate_schema("active_versions", active_versions)

    def test_canonical_url_removes_only_reviewed_tracking_parameters(self):
        first = "https://aws.amazon.com/change/?b=2&a=1&utm_source=test#fragment"
        second = "https://aws.amazon.com/change/?a=1&b=2&utm_source=test"
        self.assertEqual(validator.canonical_public_url(first), "https://aws.amazon.com/change/?b=2&a=1")
        self.assertEqual(validator.canonical_public_url(second), "https://aws.amazon.com/change/?a=1&b=2")
        self.assertNotEqual(validator.canonical_public_url(first), validator.canonical_public_url(second))
        self.assertEqual(
            validator.canonical_public_url("https://aws.amazon.com/change/?value=a+b&UTM%5Fsource=test"),
            "https://aws.amazon.com/change/?value=a+b",
        )

    def test_feed_formats_cover_rss_and_atom_only(self):
        config = copy.deepcopy(self.documents["config"])
        config["feeds"][0]["source_type"] = "public_atom"
        self.validate_schema("config", config)
        config["feeds"][0]["source_type"] = "public_json"
        with self.assertRaisesRegex(ValueError, "is not one of"):
            self.validate_schema("config", config)

    def test_null_framed_identity_fields_reject_null_characters(self):
        with self.assertRaisesRegex(ValueError, "cannot contain null characters"):
            identity.digest_parts("candidate:v3", "unsafe\0field")

    def test_queue_dispatch_identity_is_stable_per_generation(self):
        request_id = self.documents["delivery_request"]["request_id"]
        self.assertEqual(
            identity.queue_dispatch_id(request_id, 1),
            "d3dcad09334f4be3ca0943dae4ec17d8b0b233db4d066a42c92cd8bd66001f91",
        )
        self.assertEqual(
            identity.queue_dispatch_id(request_id, 2),
            "386a19c8060fd622087a3094f22b3937ee8e1f6b53ae0ae617dfed99ddb1bda0",
        )
        with self.assertRaisesRegex(ValueError, "positive integer"):
            identity.queue_dispatch_id(request_id, 0)
        with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
            identity.queue_dispatch_id(request_id.upper(), 1)

    def test_rfc3339_timestamp_parser_accepts_schema_valid_utc_suffixes(self):
        self.assertEqual(
            validator.parsed_timestamp("2026-07-13T16:00:00Z"),
            validator.parsed_timestamp("2026-07-13T16:00:00z"),
        )

    def test_removed_health_cost_and_feed_priority_fields_are_rejected(self):
        mutations = {
            "deployment health": lambda document: document.__setitem__("health_access_mode", "disabled"),
            "config cost": lambda document: document.__setitem__("cost_trends", {"enabled": False}),
            "feed priority": lambda document: document["feeds"][0].__setitem__("priority", "high"),
            "confirmed-impact message limit": lambda document: document["message_policy"].__setitem__(
                "max_affected_environments_in_slack", 30
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                name = "deployment" if label == "deployment health" else "config"
                document = copy.deepcopy(self.documents[name])
                mutate(document)
                with self.assertRaisesRegex(ValueError, "Additional properties are not allowed"):
                    self.validate_schema(name, document)

    def test_whitespace_only_policy_values_are_rejected(self):
        mutations = {
            "display": lambda config: config["services"]["eks"].__setitem__("display_name", "   "),
            "alias": lambda config: config["services"]["eks"].__setitem__("aliases", ["   "]),
            "action": lambda config: config["services"]["eks"].__setitem__("recommended_action", "   "),
            "term": lambda config: config["risk_rules"][0]["match"].__setitem__("any", ["   "]),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                config = copy.deepcopy(self.documents["config"])
                mutate(config)
                with self.assertRaisesRegex(ValueError, "does not match"):
                    self.validate_schema("config", config)

    def test_invalid_s3_bucket_names_are_rejected(self):
        invalid_names = (
            "example..com",
            "192.168.5.4",
            "xn--bucket",
            "sthree-bucket",
            "amzn-s3-demo-bucket",
            "bucket-s3alias",
            "bucket--ol-s3",
            "bucket.mrap",
            "bucket--x-s3",
            "bucket--table-s3",
            "bucket-an",
        )
        for bucket_name in invalid_names:
            with self.subTest(bucket_name=bucket_name):
                deployment = copy.deepcopy(self.documents["deployment"])
                deployment["config_bucket_name"] = bucket_name
                with self.assertRaises(ValueError):
                    self.validate_schema("deployment", deployment)

    def test_duplicate_feed_name_and_url_are_rejected(self):
        for field, message in (("name", "feed names"), ("url", "feed URLs")):
            with self.subTest(field=field):
                documents = load_documents()
                duplicate = copy.deepcopy(documents["config"]["feeds"][0])
                if field == "name":
                    duplicate["url"] = "https://aws.amazon.com/blogs/architecture/feed/"
                else:
                    duplicate["name"] = "duplicate-url"
                documents["config"]["feeds"].append(duplicate)
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_contract(documents)

    def test_disallowed_feed_host_and_ip_literal_are_rejected(self):
        cases = {
            "host": "https://example.com/feed/",
            "ip": "https://127.0.0.1/feed/",
            "fragment": "https://aws.amazon.com/feed/#latest",
        }
        for label, url in cases.items():
            with self.subTest(label=label):
                documents = load_documents()
                documents["config"]["feeds"][0]["url"] = url
                if label == "ip":
                    documents["deployment"]["feed_fetch_policy"]["allowed_feed_hosts"].append("127.0.0.1")
                with self.assertRaisesRegex(ValueError, "not approved|IP literal|fragment"):
                    self.validate_contract(documents)

    def test_publication_rejects_every_feed_url_the_runtime_rejects(self):
        cases = {
            "uppercase hostname": "https://AWS.AMAZON.COM/feed/",
            "path over runtime limit": "https://aws.amazon.com/" + "p" * 513,
            "query over runtime limit": "https://aws.amazon.com/feed/?q=" + "q" * 513,
            "raw link delimiters": "https://aws.amazon.com/feed/> <https://evil.example|Click",
        }
        for label, url in cases.items():
            with self.subTest(label=label):
                documents = load_documents()
                documents["config"]["feeds"][0]["url"] = url
                with self.assertRaisesRegex(ValueError, "lowercase|path exceeds|query exceeds|percent-encoded"):
                    validator.validate_semantics(documents["deployment"], documents["config"], documents["inventory"])

    def test_unused_or_invalid_allowed_feed_hosts_are_rejected(self):
        cases = {
            "unused": ("example.com", "unused"),
            "invalid": ("aws..amazon.com", "valid DNS hostname"),
        }
        for label, (host, message) in cases.items():
            with self.subTest(label=label):
                documents = load_documents()
                documents["deployment"]["feed_fetch_policy"]["allowed_feed_hosts"].append(host)
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_contract(documents)

    def test_service_alias_rules_are_enforced(self):
        cases = {
            "generic": (lambda config: config["services"]["eks"].__setitem__("aliases", ["version"]), "generic"),
            "collision": (
                lambda config: config["services"]["rds"]["aliases"].append("amazon eks"),
                "alias collision",
            ),
            "risk overlap": (
                lambda config: config["risk_rules"][0]["match"].__setitem__("any", ["Amazon EKS"]),
                "risk terms cannot equal",
            ),
        }
        for label, (mutate, message) in cases.items():
            with self.subTest(label=label):
                documents = load_documents()
                mutate(documents["config"])
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_contract(documents)

    def test_profile_references_and_unused_services_are_rejected(self):
        cases = {
            "unknown service": (
                lambda config: config["service_profiles"]["standard-customer-stack"]["service_ids"].append("unknown"),
                "unknown service",
            ),
            "unused service": (
                lambda config: config["service_profiles"]["standard-customer-stack"]["service_ids"].remove("eks"),
                "unused",
            ),
        }
        for label, (mutate, message) in cases.items():
            with self.subTest(label=label):
                documents = load_documents()
                mutate(documents["config"])
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_contract(documents)

    def test_environment_policy_must_cover_inventory_exactly(self):
        cases = {
            "missing": lambda config: config["environment_policies"].pop("acme-prod"),
            "unknown": lambda config: config["environment_policies"].__setitem__(
                "unknown-prod", {"feed_monitoring": "disabled", "reason": "Not managed"}
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                documents = load_documents()
                mutate(documents["config"])
                with self.assertRaisesRegex(ValueError, "cover inventory exactly"):
                    self.validate_contract(documents)

    def test_unknown_environment_profile_is_rejected(self):
        self.documents["config"]["environment_policies"]["acme-prod"]["profile"] = "unknown"
        with self.assertRaisesRegex(ValueError, "unknown profile"):
            self.validate_contract()

    def test_explicit_disabled_environment_policy_is_valid(self):
        self.documents["config"]["environment_policies"]["acme-prod"] = {
            "feed_monitoring": "disabled",
            "reason": "Environment is outside the managed service scope",
        }
        self.validate_contract()

    def test_risk_rule_identity_and_term_sets_are_enforced(self):
        mutations = {
            "id": (
                lambda rules: rules.append(copy.deepcopy(rules[0])),
                "risk rule IDs",
            ),
            "type": (
                lambda rules: rules.append({**copy.deepcopy(rules[0]), "id": "another-id"}),
                "risk types",
            ),
            "positive": (
                lambda rules: rules[0].__setitem__("match", {"any": [], "all": [], "none": []}),
                "positive term",
            ),
            "positive duplicate": (
                lambda rules: rules[0]["match"].__setitem__("all", [rules[0]["match"]["any"][0]]),
                "across any and all",
            ),
            "excluded duplicate": (
                lambda rules: rules[0]["match"].__setitem__("none", [rules[0]["match"]["any"][0]]),
                "positive and excluded",
            ),
            "normalized duplicate": (
                lambda rules: rules[0]["match"].__setitem__("any", ["end of support", "END  OF SUPPORT"]),
                "repeats normalized terms",
            ),
        }
        for label, (mutate, message) in mutations.items():
            with self.subTest(label=label):
                documents = load_documents()
                mutate(documents["config"]["risk_rules"])
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_contract(documents)

    def test_runtime_deployment_key_is_rejected(self):
        self.documents["config"]["services"]["eks"]["account_id"] = "123456789012"
        with self.assertRaisesRegex(ValueError, "deployment-owned keys"):
            validator.validate_semantics(
                self.documents["deployment"], self.documents["config"], self.documents["inventory"]
            )

    def test_unknown_route_and_duplicate_environment_are_rejected(self):
        cases = {
            "route": (
                lambda deployment: deployment["environments"][0].__setitem__("route_id", "missing"),
                "unknown route",
            ),
            "environment": (
                lambda deployment: deployment["environments"][1].__setitem__("id", deployment["environments"][0]["id"]),
                "environment IDs",
            ),
        }
        for label, (mutate, message) in cases.items():
            with self.subTest(label=label):
                documents = load_documents()
                mutate(documents["deployment"])
                sync_inventory(documents)
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_contract(documents)

    def test_slack_destinations_and_secrets_are_unique(self):
        for field, message in (
            ("destination_key", "destination is shared"),
            ("credential_secret_id", "secret is shared"),
        ):
            with self.subTest(field=field):
                documents = load_documents()
                route = copy.deepcopy(documents["deployment"]["slack"]["routes"]["shared-alerts"])
                route["channel_label"] = "#second-channel"
                route["destination_key"] = "second-channel"
                route["credential_secret_id"] = "aws-public-change-alerting/slack/second"
                route[field] = documents["deployment"]["slack"]["routes"]["shared-alerts"][field]
                documents["deployment"]["slack"]["routes"]["second-route"] = route
                sync_inventory(documents)
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_contract(documents)

    def test_slack_webhook_host_cannot_be_ip_literal(self):
        for host, message in (("127.0.0.1", "IP literal"), ("hooks..slack.com", "valid DNS hostname")):
            with self.subTest(host=host):
                documents = load_documents()
                documents["deployment"]["slack"]["approved_webhook_hosts"] = [host]
                sync_inventory(documents)
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_contract(documents)

    def test_bot_token_mode_requires_canonical_destination_key(self):
        documents = load_documents()
        slack = documents["deployment"]["slack"]
        slack["delivery_mode"] = "bot_token"
        slack.pop("approved_webhook_hosts")
        slack["workspace_id"] = "T0123456789"
        slack["bot_token_secret_id"] = "aws-public-change-alerting/slack/bot-token"
        route = slack["routes"]["shared-alerts"]
        route.pop("credential_secret_id")
        route["channel_id"] = "C0123456789"
        route["destination_key"] = "wrong-destination"
        sync_inventory(documents)
        with self.assertRaisesRegex(ValueError, "differs from workspace and channel"):
            self.validate_contract(documents)

    def test_slack_retry_and_capacity_bounds_are_enforced(self):
        cases = {
            "receive count": (
                lambda deployment: deployment["slack"]["rate_control"].__setitem__("queue_max_receive_count", 10),
                "leave room",
            ),
            "global capacity": (
                lambda deployment: deployment["scale_envelope"].__setitem__("max_delivery_requests_per_hour", 301),
                "destination capacity",
            ),
            "pacing": (
                lambda deployment: (
                    deployment["slack"]["rate_control"].__setitem__("per_destination_min_interval_seconds", 60),
                    deployment["scale_envelope"].__setitem__("max_delivery_requests_per_hour", 60),
                    deployment["scale_envelope"].__setitem__("max_delivery_requests_per_destination_per_hour", 61),
                ),
                "Slack pacing",
            ),
            "worker capacity": (
                lambda deployment: (
                    deployment["slack"]["rate_control"].__setitem__("slack_request_timeout_seconds", 30),
                    deployment["slack"]["rate_control"].__setitem__("worker_reserved_concurrency", 1),
                ),
                "timeout-derived worker upper bound",
            ),
        }
        for label, (mutate, message) in cases.items():
            with self.subTest(label=label):
                documents = load_documents()
                mutate(documents["deployment"])
                sync_inventory(documents)
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_contract(documents)

    def test_message_policy_must_render_every_release_reachable_maximum(self):
        documents = load_documents()
        documents["config"]["message_policy"]["max_summary_characters"] = 1200

        with self.assertRaisesRegex(ValueError, "cannot render its reachable maxima"):
            self.validate_contract(documents)

    def test_message_policy_must_hold_the_required_block_structure(self):
        documents = load_documents()
        documents["config"]["message_policy"]["max_blocks"] = 1

        with self.assertRaisesRegex(ValueError, "max_blocks cannot hold"):
            self.validate_contract(documents)

    def test_feed_redirects_are_disabled(self):
        deployment = copy.deepcopy(self.documents["deployment"])
        deployment["feed_fetch_policy"]["max_redirects"] = 1
        with self.assertRaisesRegex(ValueError, "0 was expected"):
            self.validate_schema("deployment", deployment)

    def test_scale_envelope_breach_is_rejected(self):
        self.documents["deployment"]["scale_envelope"]["max_feeds"] = 3
        with self.assertRaisesRegex(ValueError, "max_feeds exceeded"):
            self.validate_contract()

    def test_state_and_release_retention_invariants_are_enforced(self):
        cases = {
            "feed": (
                lambda documents: documents["config"]["state_retention"].__setitem__("feed_state_ttl_days", 364),
                "feed state retention",
            ),
            "announcement": (
                lambda documents: documents["config"]["state_retention"].__setitem__(
                    "announcement_state_ttl_days", 364
                ),
                "announcement state retention",
            ),
            "release": (
                lambda documents: documents["deployment"]["s3_lifecycle"].__setitem__(
                    "retired_release_retention_days", 364
                ),
                "retired release retention",
            ),
        }
        for label, (mutate, message) in cases.items():
            with self.subTest(label=label):
                documents = load_documents()
                mutate(documents)
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_contract(documents)

    def test_runtime_object_keys_and_inventory_projection_are_enforced(self):
        self.documents["deployment"]["active_versions_object_key"] = (
            "aws-public-change-alerting/releases/active-versions.json"
        )
        with self.assertRaisesRegex(ValueError, "immutable release prefix"):
            self.validate_contract()

        documents = load_documents()
        documents["inventory"]["environments"][0]["customer"] = "Changed"
        with self.assertRaisesRegex(ValueError, "inventory environment projection differs"):
            self.validate_contract(documents)

    def test_manifest_identity_keys_and_hashes_are_enforced(self):
        cases = {
            "key": "key differs",
            "release": "release_id differs",
            "hash": "hash differs",
            "schema": "schema_version differs",
            "time": "promotion predates inventory generation",
        }
        for label, message in cases.items():
            with self.subTest(label=label):
                documents = load_documents()
                manifest = documents["active_versions"]
                if label == "key":
                    manifest["config"]["key"] = "other/config.yaml"
                elif label == "release":
                    manifest["release_id"] = "f" * 64
                    manifest["config"]["key"] = f"aws-public-change-alerting/releases/{'f' * 64}/config.yaml"
                    manifest["inventory"]["key"] = f"aws-public-change-alerting/releases/{'f' * 64}/inventory.json"
                elif label == "hash":
                    manifest["config"]["sha256"] = "0" * 64
                    manifest["release_id"] = identity.digest_parts(
                        "release:v1", manifest["config"]["sha256"], manifest["inventory"]["sha256"]
                    )
                    release_root = f"aws-public-change-alerting/releases/{manifest['release_id']}"
                    manifest["config"]["key"] = f"{release_root}/config.yaml"
                    manifest["inventory"]["key"] = f"{release_root}/inventory.json"
                elif label == "schema":
                    manifest["config"]["schema_version"] = 2
                else:
                    manifest["promoted_at"] = "2026-07-13T15:59:59Z"
                with self.assertRaisesRegex(ValueError, message):
                    validator.validate_manifest(ROOT, documents["deployment"], manifest)

    def test_event_contract_versions_and_credential_fields_are_rejected(self):
        cases = {
            "candidate version": ("alert_candidate", "contract_version", 2, "3 was expected"),
            "delivery version": ("delivery_request", "contract_version", 2, "3 was expected"),
            "candidate credential": (
                "alert_candidate",
                "credential_secret_id",
                "secret",
                "Additional properties are not allowed",
            ),
            "delivery credential": (
                "delivery_request",
                "credential_secret_id",
                "secret",
                "Additional properties are not allowed",
            ),
        }
        for label, (name, field, value, message) in cases.items():
            with self.subTest(label=label):
                document = copy.deepcopy(self.documents[name])
                document[field] = value
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_schema(name, document)

    def test_candidate_application_version_must_be_an_artifact_digest(self):
        candidate = copy.deepcopy(self.documents["alert_candidate"])
        candidate["release"]["application_version"] = "1.2.3"

        with self.assertRaisesRegex(ValueError, "sha256"):
            self.validate_schema("alert_candidate", candidate)

    def test_candidate_announcement_identity_chain_is_enforced(self):
        mutations = {
            "announcement": ("announcement_id", "canonical announcement URL"),
            "content": ("content_fingerprint", "normalized announcement content"),
            "revision": ("revision_id", "announcement identity and content"),
            "audience": ("audience_fingerprint", "sorted candidate environment IDs"),
            "candidate": ("candidate_id", "canonical identity fields"),
        }
        for label, (field, message) in mutations.items():
            with self.subTest(label=label):
                candidate = copy.deepcopy(self.documents["alert_candidate"])
                if field in {"candidate_id", "audience_fingerprint"}:
                    candidate[field] = "f" * 64
                else:
                    candidate["announcement"][field] = "f" * 64
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_candidate(candidate)

    def test_audience_change_creates_new_candidate_and_request_identity(self):
        original = copy.deepcopy(self.documents["alert_candidate"])
        changed = copy.deepcopy(original)
        changed["environment_ids"] = changed["environment_ids"][:-1]
        sync_candidate_identity(changed)
        self.assertNotEqual(changed["audience_fingerprint"], original["audience_fingerprint"])
        self.assertNotEqual(changed["candidate_id"], original["candidate_id"])
        original_request_id = identity.digest_parts("delivery-request:v2", original["candidate_id"])
        changed_request_id = identity.digest_parts("delivery-request:v2", changed["candidate_id"])
        self.assertNotEqual(changed_request_id, original_request_id)

    def test_candidate_title_over_the_schema_bound_is_rejected(self):
        candidate = copy.deepcopy(self.documents["alert_candidate"])
        candidate["announcement"]["title"] = "x" * 301
        with self.assertRaises(ValueError):
            self.validate_schema("alert_candidate", candidate)

    def test_candidate_and_delivery_timestamp_order_is_enforced(self):
        candidate = copy.deepcopy(self.documents["alert_candidate"])
        candidate["created_at"] = "2026-07-13T16:58:59Z"
        with self.assertRaisesRegex(ValueError, "predates announcement observation"):
            self.validate_candidate(candidate)

        candidate = copy.deepcopy(self.documents["alert_candidate"])
        candidate["created_at"] = "2026-07-13T16:29:59Z"
        candidate["announcement"]["observed_at"] = "2026-07-13T16:29:58Z"
        with self.assertRaisesRegex(ValueError, "predates its release promotion"):
            self.validate_candidate(candidate)

        request = copy.deepcopy(self.documents["delivery_request"])
        request["created_at"] = "2026-07-13T16:59:59Z"
        with self.assertRaisesRegex(ValueError, "predates its candidate"):
            validator.validate_event_contract_semantics(
                self.documents["config"],
                self.documents["inventory"],
                self.documents["active_versions"],
                self.documents["alert_candidate"],
                request,
            )

    def test_candidate_publication_time_must_be_normalized_but_may_be_future_dated(self):
        candidate = copy.deepcopy(self.documents["alert_candidate"])
        candidate["announcement"]["published_at"] = "2026-07-13T08:00:00-04:00"
        with self.assertRaisesRegex(ValueError, "published_at is not a normalized UTC timestamp"):
            self.validate_candidate(candidate)

        candidate = copy.deepcopy(self.documents["alert_candidate"])
        candidate["announcement"]["published_at"] = "2099-01-01T00:00:00Z"
        self.validate_candidate(candidate)

    def test_candidate_release_and_config_values_are_enforced(self):
        mutations = {
            "release": (
                lambda candidate: candidate["release"].__setitem__("release_id", "f" * 64),
                "release differs",
            ),
            "service": (
                lambda candidate: candidate["service"].__setitem__("display_name", "Wrong"),
                "display name",
            ),
            "risk": (
                lambda candidate: candidate["risk"].__setitem__("priority", "high"),
                "priority differs",
            ),
            "action": (
                lambda candidate: candidate.__setitem__("recommended_action", "Wrong action"),
                "recommended action",
            ),
            "alias": (
                lambda candidate: candidate["explainability"].__setitem__("matched_aliases", ["Unknown"]),
                "matched aliases",
            ),
            "term": (
                lambda candidate: candidate["explainability"].__setitem__("matched_terms", ["Unknown"]),
                "matched terms",
            ),
        }
        for label, (mutate, message) in mutations.items():
            with self.subTest(label=label):
                candidate = copy.deepcopy(self.documents["alert_candidate"])
                mutate(candidate)
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_candidate(candidate)

    def test_candidate_mapping_and_order_are_enforced(self):
        cases = {
            "order": (
                lambda documents: documents["alert_candidate"].__setitem__(
                    "environment_ids", list(reversed(documents["alert_candidate"]["environment_ids"]))
                ),
                "complete route and profile mapping",
            ),
            # Moves an environment to a second, valid route. Pointing it at a
            # route that does not exist would be rejected as an unknown route
            # before the audience is ever compared, which is the case below and
            # not the exclusion this one is here to prove.
            "route": (
                move_environment_to_new_route,
                "complete route and profile mapping",
            ),
            "unknown route": (
                lambda documents: documents["inventory"]["environments"][0].__setitem__("route_id", "other-route"),
                "names unknown route",
            ),
            "disabled": (
                lambda documents: documents["config"]["environment_policies"].__setitem__(
                    "acme-prod", {"feed_monitoring": "disabled", "reason": "Not monitored"}
                ),
                "complete route and profile mapping",
            ),
            "profile": (
                lambda documents: documents["alert_candidate"]["explainability"].__setitem__(
                    "matched_profile_ids", ["unknown-profile"]
                ),
                "complete environment mapping",
            ),
            "missing environment": (
                lambda documents: documents["alert_candidate"].__setitem__(
                    "environment_ids", documents["alert_candidate"]["environment_ids"][:-1]
                ),
                "complete route and profile mapping",
            ),
        }
        for label, (mutate, message) in cases.items():
            with self.subTest(label=label):
                documents = load_documents()
                mutate(documents)
                if label == "missing environment":
                    sync_candidate_identity(documents["alert_candidate"])
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_candidate(documents=documents)

    def test_candidate_provenance_is_enforced(self):
        candidate = copy.deepcopy(self.documents["alert_candidate"])
        candidate["announcement"]["provenance"][0]["feed_name"] = "unknown-feed"
        with self.assertRaisesRegex(ValueError, "unknown feed"):
            self.validate_candidate(candidate)

        candidate = copy.deepcopy(self.documents["alert_candidate"])
        candidate["announcement"]["provenance"][0]["feed_url"] = "https://aws.amazon.com/blogs/architecture/feed/"
        with self.assertRaisesRegex(ValueError, "URL differs"):
            self.validate_candidate(candidate)

        candidate = copy.deepcopy(self.documents["alert_candidate"])
        candidate["announcement"]["provenance"][0]["source_item_url"] = (
            "https://aws.amazon.com/about-aws/whats-new/2026/different-item/"
        )
        with self.assertRaisesRegex(ValueError, "differs from the canonical announcement URL"):
            self.validate_candidate(candidate)

        candidate = copy.deepcopy(self.documents["alert_candidate"])
        candidate["announcement"]["provenance"][0]["source_item_url"] += "#overview"
        # Accepted, unlike every other URL this validator owns: the sighting
        # records what the feed published, and the rule above requires only that
        # it canonicalize to the announcement URL. The runtime emits this form,
        # which `test_pipeline` drives through the whole chain.
        self.validate_candidate(candidate)

        for item_url in (
            "https://AWS.AMAZON.COM/about-aws/whats-new/2026/example-eks-update/",
            "https://aws.amazon.com:443/about-aws/whats-new/2026/example-eks-update/",
        ):
            with self.subTest(canonicalizable_source_item_url=item_url):
                candidate = copy.deepcopy(self.documents["alert_candidate"])
                candidate["announcement"]["provenance"][0]["source_item_url"] = item_url
                self.validate_candidate(candidate)

        candidate = copy.deepcopy(self.documents["alert_candidate"])
        candidate["announcement"]["provenance"][0]["source_item_url"] = (
            "https://user:pw@aws.amazon.com/about-aws/whats-new/2026/example-eks-update/"
        )
        with self.assertRaisesRegex(ValueError, "user-info component"):
            self.validate_candidate(candidate)

        candidate = copy.deepcopy(self.documents["alert_candidate"])
        candidate["announcement"]["provenance"][0]["source_item_url"] = (
            "https://@aws.amazon.com/about-aws/whats-new/2026/example-eks-update/"
        )
        # Blank credentials are still a user-info component; `parsed.username`
        # is the empty string here and a username check alone would pass it.
        with self.assertRaisesRegex(ValueError, "user-info component"):
            self.validate_candidate(candidate)

        candidate = copy.deepcopy(self.documents["alert_candidate"])
        candidate["announcement"]["url"] = "https://example.com/announcement/"
        sync_candidate_identity(candidate)
        with self.assertRaisesRegex(ValueError, "hostname is not approved"):
            self.validate_candidate(candidate)

    def test_candidate_claimed_match_evidence_is_enforced(self):
        cases = {
            "alias": (
                lambda candidate: (
                    candidate["announcement"].__setitem__("title", "Kubernetes version 1.34 available"),
                    candidate["announcement"].__setitem__(
                        "summary", "Kubernetes version 1.34 is supported in all configured Regions."
                    ),
                ),
                "service evidence",
            ),
            "risk": (
                lambda candidate: (
                    candidate["announcement"].__setitem__("title", "Amazon EKS documentation available"),
                    candidate["announcement"].__setitem__(
                        "summary", "Amazon EKS documentation is available in all configured Regions."
                    ),
                ),
                "does not satisfy its risk rule",
            ),
            "reported term": (
                lambda candidate: candidate["explainability"].__setitem__("matched_terms", ["runtime update"]),
                "risk evidence",
            ),
            "reported fields": (
                lambda candidate: candidate["explainability"].__setitem__("matched_fields", ["title"]),
                "matched fields differ",
            ),
        }
        for label, (mutate, message) in cases.items():
            with self.subTest(label=label):
                candidate = copy.deepcopy(self.documents["alert_candidate"])
                mutate(candidate)
                if label in {"alias", "risk"}:
                    sync_candidate_identity(candidate)
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_candidate(candidate)

    def test_candidate_service_and_risk_evidence_must_be_distinct(self):
        documents = load_documents()
        rule = next(
            item for item in documents["config"]["risk_rules"] if item["id"] == "watched-service-version-update"
        )
        rule["match"]["any"] = ["EKS"]
        candidate = documents["alert_candidate"]
        candidate["explainability"]["matched_terms"] = ["EKS"]
        with self.assertRaisesRegex(ValueError, "must be distinct"):
            self.validate_candidate(documents=documents)

    def test_candidate_size_limit_is_enforced(self):
        candidate = copy.deepcopy(self.documents["alert_candidate"])
        candidate["explainability"]["reason"] = "x" * 197000
        with self.assertRaisesRegex(ValueError, "alert candidate exceeds"):
            self.validate_candidate(candidate)

    def test_delivery_embeds_exact_candidate_and_identity(self):
        request = copy.deepcopy(self.documents["delivery_request"])
        request["candidate"]["announcement"]["title"] = "Changed"
        with self.assertRaisesRegex(ValueError, "embedded candidate differs"):
            validator.validate_event_contract_semantics(
                self.documents["config"],
                self.documents["inventory"],
                self.documents["active_versions"],
                self.documents["alert_candidate"],
                request,
            )

        request = copy.deepcopy(self.documents["delivery_request"])
        request["request_id"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "request_id differs"):
            validator.validate_event_contract_semantics(
                self.documents["config"],
                self.documents["inventory"],
                self.documents["active_versions"],
                self.documents["alert_candidate"],
                request,
            )

    def test_delivery_destination_and_size_are_enforced(self):
        request = copy.deepcopy(self.documents["delivery_request"])
        request["destination_key"] = "wrong-destination"
        with self.assertRaisesRegex(ValueError, "destination_key differs"):
            validator.validate_event_contract_semantics(
                self.documents["config"],
                self.documents["inventory"],
                self.documents["active_versions"],
                self.documents["alert_candidate"],
                request,
            )

        config = copy.deepcopy(self.documents["config"])
        config["message_policy"]["max_delivery_request_bytes"] = 1000
        with self.assertRaisesRegex(ValueError, "delivery request exceeds"):
            validator.validate_event_contract_semantics(
                config,
                self.documents["inventory"],
                self.documents["active_versions"],
                self.documents["alert_candidate"],
                self.documents["delivery_request"],
            )


class DeploymentInputTests(unittest.TestCase):
    """Cover the deployment files Terraform roots decode, not just the example."""

    def test_committed_deployment_inputs_pass_the_schema(self):
        validator.validate_deployment_inputs(ROOT)

    def _staged_root(self, directory, deployment):
        """Build a throwaway root holding schemas and one deployment input."""

        root = Path(directory)
        (root / "schemas").mkdir()
        for schema in (ROOT / "schemas").glob("*.json"):
            (root / "schemas" / schema.name).write_text(schema.read_text(), encoding="utf-8")
        target = root / validator.DEPLOYMENT_INPUTS[0]
        target.parent.mkdir(parents=True)
        if deployment is not None:
            target.write_text(yaml.safe_dump(deployment), encoding="utf-8")
        return root

    def test_invalid_deployment_input_is_rejected(self):
        deployment = validator.load_yaml(ROOT / validator.DEPLOYMENT_INPUTS[0])
        deployment["log_retention_days"] = 0
        with tempfile.TemporaryDirectory() as directory:
            root = self._staged_root(directory, deployment)
            with self.assertRaisesRegex(ValueError, "log_retention_days"):
                validator.validate_deployment_inputs(root)

    def test_unknown_field_in_deployment_input_is_rejected(self):
        deployment = validator.load_yaml(ROOT / validator.DEPLOYMENT_INPUTS[0])
        deployment["raw_snapshot_prefix"] = "apcf/raw-snapshots/"
        with tempfile.TemporaryDirectory() as directory:
            root = self._staged_root(directory, deployment)
            with self.assertRaisesRegex(ValueError, "raw_snapshot_prefix"):
                validator.validate_deployment_inputs(root)

    def test_operational_sns_subscriptions_are_required(self):
        deployment = validator.load_yaml(ROOT / validator.DEPLOYMENT_INPUTS[0])
        del deployment["operational_sns_subscriptions"]
        with tempfile.TemporaryDirectory() as directory:
            root = self._staged_root(directory, deployment)
            with self.assertRaisesRegex(ValueError, "operational_sns_subscriptions"):
                validator.validate_deployment_inputs(root)

    def test_operational_sns_subscription_rejects_unknown_fields(self):
        deployment = validator.load_yaml(ROOT / validator.DEPLOYMENT_INPUTS[0])
        deployment["operational_sns_subscriptions"][0]["endpoint"] = "operator@example.invalid"
        with tempfile.TemporaryDirectory() as directory:
            root = self._staged_root(directory, deployment)
            with self.assertRaisesRegex(ValueError, "endpoint"):
                validator.validate_deployment_inputs(root)

    def test_operational_sns_subscription_aliases_are_unique(self):
        deployment = validator.load_yaml(ROOT / validator.DEPLOYMENT_INPUTS[0])
        deployment["operational_sns_subscriptions"].append(deployment["operational_sns_subscriptions"][0].copy())
        with tempfile.TemporaryDirectory() as directory:
            root = self._staged_root(directory, deployment)
            with self.assertRaisesRegex(ValueError, "non-unique"):
                validator.validate_deployment_inputs(root)

    def test_operational_sns_subscription_protocol_is_email(self):
        deployment = validator.load_yaml(ROOT / validator.DEPLOYMENT_INPUTS[0])
        deployment["operational_sns_subscriptions"][0]["protocol"] = "https"
        with tempfile.TemporaryDirectory() as directory:
            root = self._staged_root(directory, deployment)
            with self.assertRaisesRegex(ValueError, "email"):
                validator.validate_deployment_inputs(root)

    def test_committed_operational_sns_descriptors_contain_no_endpoint(self):
        for path in validator.DEPLOYMENT_INPUTS:
            with self.subTest(path=path):
                deployment = validator.load_yaml(ROOT / path)
                for subscription in deployment["operational_sns_subscriptions"]:
                    self.assertEqual(set(subscription), {"alias", "protocol"})

    def test_application_package_retirement_is_required(self):
        deployment = validator.load_yaml(ROOT / validator.DEPLOYMENT_INPUTS[0])
        del deployment["application_package_retirement"]
        with tempfile.TemporaryDirectory() as directory:
            root = self._staged_root(directory, deployment)
            with self.assertRaisesRegex(ValueError, "application_package_retirement"):
                validator.validate_deployment_inputs(root)

    def test_application_package_retirement_rejects_unknown_fields(self):
        deployment = validator.load_yaml(ROOT / validator.DEPLOYMENT_INPUTS[0])
        deployment["application_package_retirement"]["use_release_policy"] = True
        with tempfile.TemporaryDirectory() as directory:
            root = self._staged_root(directory, deployment)
            with self.assertRaisesRegex(ValueError, "use_release_policy"):
                validator.validate_deployment_inputs(root)

    def test_application_package_retention_floor_is_enforced(self):
        deployment = validator.load_yaml(ROOT / validator.DEPLOYMENT_INPUTS[0])
        deployment["application_package_retirement"]["retention_days"] = 399
        with tempfile.TemporaryDirectory() as directory:
            root = self._staged_root(directory, deployment)
            with self.assertRaisesRegex(ValueError, "retention_days"):
                validator.validate_deployment_inputs(root)

    def test_application_package_count_floor_is_enforced(self):
        deployment = validator.load_yaml(ROOT / validator.DEPLOYMENT_INPUTS[0])
        deployment["application_package_retirement"]["minimum_retained_packages"] = 9
        with tempfile.TemporaryDirectory() as directory:
            root = self._staged_root(directory, deployment)
            with self.assertRaisesRegex(ValueError, "minimum_retained_packages"):
                validator.validate_deployment_inputs(root)

    def test_application_package_retention_ceiling_is_enforced(self):
        deployment = validator.load_yaml(ROOT / validator.DEPLOYMENT_INPUTS[0])
        deployment["application_package_retirement"]["retention_days"] = 3651
        with tempfile.TemporaryDirectory() as directory:
            root = self._staged_root(directory, deployment)
            with self.assertRaisesRegex(ValueError, "retention_days"):
                validator.validate_deployment_inputs(root)

    def test_application_package_count_ceiling_is_enforced(self):
        deployment = validator.load_yaml(ROOT / validator.DEPLOYMENT_INPUTS[0])
        deployment["application_package_retirement"]["minimum_retained_packages"] = 101
        with tempfile.TemporaryDirectory() as directory:
            root = self._staged_root(directory, deployment)
            with self.assertRaisesRegex(ValueError, "minimum_retained_packages"):
                validator.validate_deployment_inputs(root)

    def test_missing_deployment_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._staged_root(directory, None)
            with self.assertRaisesRegex(ValueError, "declared deployment input is missing"):
                validator.validate_deployment_inputs(root)


if __name__ == "__main__":
    unittest.main()
