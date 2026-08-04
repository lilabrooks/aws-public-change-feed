"""Keep the Codex and Claude Code adapters equivalent.

The same MCP server has to be declared twice because the hosts read different
files and share no format. Nothing about that duplication is self-correcting,
so a change to one file that misses the other would silently leave one agent
without the tool. These tests compare the two directly.
"""

import json
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CLAUDE_MCP = ROOT / ".mcp.json"
CODEX_CONFIG = ROOT / ".codex/config.toml"
CLAUDE_MD = ROOT / "CLAUDE.md"
AGENTS_MD = ROOT / "AGENTS.md"
CLAUDE_SETTINGS = ROOT / ".claude/settings.json"

# The AWS MCP server exposes account-capable tools alongside the documentation
# tools this repository uses. They fail unauthenticated, which is a property of
# nobody having signed in rather than of the server, so they are denied outright
# on the host that can deny them.
ACCOUNT_CAPABLE_TOOLS = ("call_aws", "run_script", "get_presigned_url", "get_tasks")


def claude_servers() -> dict[str, str]:
    with CLAUDE_MCP.open(encoding="utf-8") as handle:
        document = json.load(handle)
    return {name: entry["url"] for name, entry in document["mcpServers"].items()}


def codex_servers() -> dict[str, str]:
    with CODEX_CONFIG.open("rb") as handle:
        document = tomllib.load(handle)
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
        with CLAUDE_MCP.open(encoding="utf-8") as handle:
            document = json.load(handle)
        for name, entry in document["mcpServers"].items():
            with self.subTest(server=name):
                if "url" in entry:
                    self.assertEqual(entry.get("type"), "http")

    def test_endpoints_are_https(self):
        for name, url in claude_servers().items():
            with self.subTest(server=name):
                self.assertTrue(url.startswith("https://"), f"{name} must use HTTPS")


class AccountToolDenyTests(unittest.TestCase):
    """The deny list only works while it names the server actually configured."""

    def denied(self) -> list[str]:
        with CLAUDE_SETTINGS.open(encoding="utf-8") as handle:
            return json.load(handle)["permissions"]["deny"]

    def test_every_account_capable_tool_is_denied(self):
        denied = self.denied()
        for tool in ACCOUNT_CAPABLE_TOOLS:
            with self.subTest(tool=tool):
                self.assertTrue(
                    any(rule.endswith(f"__aws___{tool}") for rule in denied),
                    f"{tool} acts on an AWS account and is not denied in .claude/settings.json",
                )

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
