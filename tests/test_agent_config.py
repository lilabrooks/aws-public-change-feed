"""Keep the Codex and Claude Code adapters equivalent.

The same MCP server has to be declared twice because the hosts read different
files and share no format. Nothing about that duplication is self-correcting,
so a change to one file that misses the other would silently leave one agent
without the tool. These tests compare the two directly.
"""

import json
import re
import sys
import tomllib
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CLAUDE_MCP = ROOT / ".mcp.json"
CODEX_CONFIG = ROOT / ".codex/config.toml"
CLAUDE_MD = ROOT / "CLAUDE.md"
AGENTS_MD = ROOT / "AGENTS.md"
CLAUDE_SETTINGS = ROOT / ".claude/settings.json"
AGENT_TOOLING_MD = ROOT / "docs/agent-tooling.md"

# The AWS MCP server exposes account-capable tools alongside the documentation
# tools this repository uses. They fail unauthenticated, which is a property of
# nobody having signed in rather than of the server, so they are denied outright
# on the host that can deny them.
ACCOUNT_CAPABLE_TOOLS = ("call_aws", "run_script", "get_presigned_url", "get_tasks")
AWS_MCP_SERVER = "aws-mcp"
AWS_MCP_ENDPOINT = "https://aws-mcp.us-east-1.api.aws/mcp"
CLAUDE_AWS_MCP_FIELDS = frozenset({"type", "url"})
CODEX_AWS_MCP_FIELDS = frozenset({"url"})
ACCOUNT_TOOL_DENIES = frozenset(f"mcp__{AWS_MCP_SERVER}__aws___{tool}" for tool in ACCOUNT_CAPABLE_TOOLS)
ENV_FILE_DENIES = frozenset({"Read(./.env)", "Read(./**/.env)"})
REQUIRED_CLAUDE_DENIES = ACCOUNT_TOOL_DENIES | ENV_FILE_DENIES
DOCUMENTED_ENDPOINT = re.compile(r"^Endpoint `([^`]+)` over HTTP,", re.MULTILINE)


def load_claude_mcp() -> dict:
    with CLAUDE_MCP.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_codex_config() -> dict:
    with CODEX_CONFIG.open("rb") as handle:
        return tomllib.load(handle)


def load_claude_settings() -> dict:
    with CLAUDE_SETTINGS.open(encoding="utf-8") as handle:
        return json.load(handle)


def _closed_aws_mcp_entry(document: dict, *, table_name: str, allowed_fields: frozenset[str]) -> dict:
    servers = document.get(table_name)
    if not isinstance(servers, dict) or set(servers) != {AWS_MCP_SERVER}:
        raise ValueError(f"{table_name} must declare only the tracked {AWS_MCP_SERVER} server")

    entry = servers[AWS_MCP_SERVER]
    if not isinstance(entry, dict) or set(entry) != allowed_fields:
        raise ValueError(f"{table_name}.{AWS_MCP_SERVER} fields must be exactly {sorted(allowed_fields)}")
    return entry


def validate_claude_mcp(document: dict) -> None:
    entry = _closed_aws_mcp_entry(
        document,
        table_name="mcpServers",
        allowed_fields=CLAUDE_AWS_MCP_FIELDS,
    )
    if entry["type"] != "http":
        raise ValueError("Claude must use HTTP transport for the tracked AWS MCP server")
    if entry["url"] != AWS_MCP_ENDPOINT:
        raise ValueError("Claude must use the exact AWS MCP endpoint approved for unauthenticated use")


def validate_codex_mcp(document: dict) -> None:
    entry = _closed_aws_mcp_entry(
        document,
        table_name="mcp_servers",
        allowed_fields=CODEX_AWS_MCP_FIELDS,
    )
    if entry["url"] != AWS_MCP_ENDPOINT:
        raise ValueError("Codex must use the exact AWS MCP endpoint approved for unauthenticated use")


def validate_claude_denies(document: dict) -> None:
    permissions = document.get("permissions")
    denied = permissions.get("deny") if isinstance(permissions, dict) else None
    if not isinstance(denied, list) or any(not isinstance(rule, str) for rule in denied):
        raise ValueError(".claude/settings.json permissions.deny must be a list of strings")
    missing = REQUIRED_CLAUDE_DENIES - set(denied)
    if missing:
        raise ValueError(f"missing exact Claude deny rules: {sorted(missing)}")


def validate_documented_endpoint(document: str) -> None:
    endpoints = DOCUMENTED_ENDPOINT.findall(document)
    if endpoints != [AWS_MCP_ENDPOINT]:
        raise ValueError("docs/agent-tooling.md must record the exact tracked AWS MCP endpoint once")


def claude_servers() -> dict[str, str]:
    document = load_claude_mcp()
    validate_claude_mcp(document)
    return {name: entry["url"] for name, entry in document["mcpServers"].items()}


def codex_servers() -> dict[str, str]:
    document = load_codex_config()
    validate_codex_mcp(document)
    return {name: entry["url"] for name, entry in document["mcp_servers"].items()}


class McpParityTests(unittest.TestCase):
    def test_both_host_configurations_exist(self):
        self.assertTrue(CLAUDE_MCP.is_file(), ".mcp.json is missing; Claude Code would lose its MCP servers")
        self.assertTrue(CODEX_CONFIG.is_file(), ".codex/config.toml is missing; Codex would lose its MCP servers")

    def test_the_same_servers_are_declared_to_both_hosts(self):
        self.assertEqual(
            sorted(claude_servers()),
            sorted(codex_servers()),
            "an MCP server is configured for one host but not the other",
        )

    def test_each_server_points_at_the_same_endpoint(self):
        self.assertEqual(
            claude_servers(),
            codex_servers(),
            "an MCP server URL differs between the Claude Code and Codex configurations",
        )

    def test_claude_declares_http_transport_for_url_servers(self):
        # Codex infers transport from the presence of `url`; Claude Code needs
        # it stated. A missing type is how the Claude side silently fails.
        document = load_claude_mcp()
        for name, entry in document["mcpServers"].items():
            with self.subTest(server=name):
                if "url" in entry:
                    self.assertEqual(entry.get("type"), "http")

    def test_endpoints_are_https(self):
        for name, url in claude_servers().items():
            with self.subTest(server=name):
                self.assertTrue(url.startswith("https://"), f"{name} must use HTTPS")


class ClosedAwsMcpContractTests(unittest.TestCase):
    """Tracked adapters cannot add credential sources or a local process."""

    def assert_extra_field_rejected(
        self,
        *,
        claude_field: str,
        claude_value: object,
        codex_field: str,
        codex_value: object,
    ) -> None:
        cases = (
            (
                "Claude",
                load_claude_mcp(),
                "mcpServers",
                validate_claude_mcp,
                claude_field,
                claude_value,
            ),
            (
                "Codex",
                load_codex_config(),
                "mcp_servers",
                validate_codex_mcp,
                codex_field,
                codex_value,
            ),
        )
        for host, document, table_name, validate, field, value in cases:
            with self.subTest(host=host, field=field):
                mutated = deepcopy(document)
                mutated[table_name][AWS_MCP_SERVER][field] = deepcopy(value)
                with self.assertRaises(ValueError):
                    validate(mutated)

    def test_tracked_adapters_match_the_closed_contract(self):
        validate_claude_mcp(load_claude_mcp())
        validate_codex_mcp(load_codex_config())

    def test_documented_endpoint_matches_the_closed_contract(self):
        validate_documented_endpoint(AGENT_TOOLING_MD.read_text(encoding="utf-8"))

    def test_rejects_changed_documented_endpoint(self):
        document = AGENT_TOOLING_MD.read_text(encoding="utf-8")
        mutated = document.replace(
            f"Endpoint `{AWS_MCP_ENDPOINT}` over HTTP,",
            "Endpoint `https://example.invalid/mcp` over HTTP,",
        )
        self.assertNotEqual(mutated, document)
        with self.assertRaises(ValueError):
            validate_documented_endpoint(mutated)

    def test_rejects_changed_server_keys(self):
        cases = (
            (load_claude_mcp(), "mcpServers", validate_claude_mcp),
            (load_codex_config(), "mcp_servers", validate_codex_mcp),
        )
        for document, table_name, validate in cases:
            with self.subTest(table=table_name):
                mutated = deepcopy(document)
                entry = mutated[table_name].pop(AWS_MCP_SERVER)
                mutated[table_name]["credentialed-aws-mcp"] = entry
                with self.assertRaises(ValueError):
                    validate(mutated)

    def test_rejects_additional_servers(self):
        cases = (
            (load_claude_mcp(), "mcpServers", validate_claude_mcp),
            (load_codex_config(), "mcp_servers", validate_codex_mcp),
        )
        for document, table_name, validate in cases:
            with self.subTest(table=table_name):
                mutated = deepcopy(document)
                mutated[table_name]["credentialed-aws-mcp"] = deepcopy(mutated[table_name][AWS_MCP_SERVER])
                with self.assertRaises(ValueError):
                    validate(mutated)

    def test_rejects_process_configuration(self):
        self.assert_extra_field_rejected(
            claude_field="command",
            claude_value="credential-wrapper",
            codex_field="command",
            codex_value="credential-wrapper",
        )

    def test_rejects_process_arguments(self):
        self.assert_extra_field_rejected(
            claude_field="args",
            claude_value=["--profile", "dev"],
            codex_field="args",
            codex_value=["--profile", "dev"],
        )

    def test_rejects_header_helpers(self):
        self.assert_extra_field_rejected(
            claude_field="headersHelper",
            claude_value="credential-wrapper",
            codex_field="http_headers_helper",
            codex_value="credential-wrapper",
        )

    def test_rejects_environment_configuration(self):
        self.assert_extra_field_rejected(
            claude_field="env",
            claude_value={"AWS_PROFILE": "dev"},
            codex_field="env",
            codex_value={"AWS_PROFILE": "dev"},
        )

    def test_rejects_static_headers(self):
        self.assert_extra_field_rejected(
            claude_field="headers",
            claude_value={"Authorization": "credential-source"},
            codex_field="http_headers",
            codex_value={"Authorization": "credential-source"},
        )

    def test_rejects_environment_backed_headers(self):
        self.assert_extra_field_rejected(
            claude_field="headers",
            claude_value={"Authorization": "${AWS_MCP_TOKEN}"},
            codex_field="env_http_headers",
            codex_value={"Authorization": "AWS_MCP_TOKEN"},
        )

    def test_rejects_token_configuration(self):
        mutated = load_codex_config()
        mutated["mcp_servers"][AWS_MCP_SERVER]["bearer_token_env_var"] = "AWS_MCP_TOKEN"
        with self.assertRaises(ValueError):
            validate_codex_mcp(mutated)

    def test_rejects_authentication_configuration(self):
        mutated = load_codex_config()
        mutated["mcp_servers"][AWS_MCP_SERVER]["auth"] = "oauth"
        with self.assertRaises(ValueError):
            validate_codex_mcp(mutated)

    def test_rejects_non_http_claude_transport(self):
        mutated = load_claude_mcp()
        mutated["mcpServers"][AWS_MCP_SERVER]["type"] = "stdio"
        with self.assertRaises(ValueError):
            validate_claude_mcp(mutated)

    def test_rejects_changed_endpoints(self):
        cases = (
            (load_claude_mcp(), "mcpServers", validate_claude_mcp),
            (load_codex_config(), "mcp_servers", validate_codex_mcp),
        )
        for document, table_name, validate in cases:
            with self.subTest(table=table_name):
                mutated = deepcopy(document)
                mutated[table_name][AWS_MCP_SERVER]["url"] = "https://example.invalid/mcp"
                with self.assertRaises(ValueError):
                    validate(mutated)


class AccountToolDenyTests(unittest.TestCase):
    """The deny list only works while it names the server actually configured."""

    def denied(self) -> list[str]:
        document = load_claude_settings()
        validate_claude_denies(document)
        return document["permissions"]["deny"]

    def test_every_account_capable_tool_is_denied(self):
        denied = self.denied()
        for tool in ACCOUNT_CAPABLE_TOOLS:
            with self.subTest(tool=tool):
                self.assertTrue(
                    f"mcp__{AWS_MCP_SERVER}__aws___{tool}" in denied,
                    f"{tool} acts on an AWS account and is not denied in .claude/settings.json",
                )

    def test_each_missing_account_tool_deny_is_rejected(self):
        document = load_claude_settings()
        for rule in ACCOUNT_TOOL_DENIES:
            with self.subTest(rule=rule):
                mutated = deepcopy(document)
                self.assertIn(rule, mutated["permissions"]["deny"])
                mutated["permissions"]["deny"] = [
                    candidate for candidate in mutated["permissions"]["deny"] if candidate != rule
                ]
                with self.assertRaises(ValueError):
                    validate_claude_denies(mutated)

    def test_each_missing_env_file_deny_is_rejected(self):
        document = load_claude_settings()
        for rule in ENV_FILE_DENIES:
            with self.subTest(rule=rule):
                mutated = deepcopy(document)
                self.assertIn(rule, mutated["permissions"]["deny"])
                mutated["permissions"]["deny"] = [
                    candidate for candidate in mutated["permissions"]["deny"] if candidate != rule
                ]
                with self.assertRaises(ValueError):
                    validate_claude_denies(mutated)

    def test_deny_rules_name_the_configured_server(self):
        # A rename in .mcp.json leaves these rules pointing at a server that no
        # longer exists, which denies nothing and looks exactly like protection.
        servers = set(claude_servers())
        for rule in self.denied():
            if not rule.startswith("mcp__"):
                continue
            with self.subTest(rule=rule):
                server = rule.split("__")[1]
                self.assertIn(
                    server,
                    servers,
                    f"{rule} denies a tool on '{server}', which is not configured in .mcp.json",
                )


class SharedInstructionTests(unittest.TestCase):
    """AGENTS.md is the shared source; CLAUDE.md is a thin adapter over it."""

    def test_claude_imports_the_shared_instructions(self):
        self.assertIn("@AGENTS.md", CLAUDE_MD.read_text(encoding="utf-8"))

    def test_every_claude_import_resolves(self):
        for line in CLAUDE_MD.read_text(encoding="utf-8").splitlines():
            target = line.strip()
            if not target.startswith("@"):
                continue
            with self.subTest(target=target):
                self.assertTrue((ROOT / target[1:]).exists(), f"{target} does not resolve")

    def test_claude_md_carries_no_shared_rules_of_its_own(self):
        # Shared guidance belongs in AGENTS.md so Codex receives it too. A
        # CLAUDE.md that grows prose is how the two hosts start to diverge.
        body = [
            line
            for line in CLAUDE_MD.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("@")
        ]
        self.assertEqual(body, [], "CLAUDE.md holds content Codex will never read; move it to AGENTS.md")

    def test_agents_md_documents_both_host_configurations(self):
        instructions = AGENTS_MD.read_text(encoding="utf-8")
        for path in (".mcp.json", ".codex/config.toml"):
            with self.subTest(path=path):
                self.assertIn(path, instructions)


if __name__ == "__main__":
    unittest.main()
