import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import generate_slack_sample as slack_sample  # noqa: E402
import validate_site as validator  # noqa: E402
import workflow_pins  # noqa: E402
import yaml  # noqa: E402


class SiteValidatorTests(unittest.TestCase):
    def make_repository(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        shutil.copytree(ROOT / "site", root / "site")
        shutil.copytree(ROOT / "examples", root / "examples")
        shutil.copy2(ROOT / "README.md", root / "README.md")
        return directory, root

    def test_committed_site_passes_validation(self):
        self.assertEqual(validator.validate_repository(ROOT), [])

    def test_committed_slack_sample_contains_the_canonical_candidate(self):
        candidate = json.loads((ROOT / "examples/alert-candidate.json").read_text(encoding="utf-8"))
        page = (ROOT / "site/index.html").read_text(encoding="utf-8")
        self.assertIn(candidate["candidate_id"], page)
        self.assertEqual(slack_sample.validation_errors(ROOT), [])

    def test_hand_edited_slack_sample_is_rejected(self):
        directory, root = self.make_repository()
        with directory:
            page = root / "site/index.html"
            source = page.read_text(encoding="utf-8")
            candidate = json.loads((root / "examples/alert-candidate.json").read_text(encoding="utf-8"))
            page.write_text(source.replace(candidate["candidate_id"], "0" * 64, 1), encoding="utf-8")
            errors = validator.validate_repository(root)
        self.assertTrue(any("renderer-generated Slack sample is stale" in error for error in errors))

    def test_canonical_fixture_change_makes_the_sample_stale(self):
        directory, root = self.make_repository()
        with directory:
            candidate_path = root / "examples/alert-candidate.json"
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            candidate["announcement"]["title"] = "Changed canonical title"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            errors = validator.validate_repository(root)
        self.assertTrue(any("renderer-generated Slack sample is stale" in error for error in errors))

    def test_slack_sample_refuses_a_source_outside_the_approved_hosts(self):
        directory, root = self.make_repository()
        with directory:
            candidate_path = root / "examples/alert-candidate.json"
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            candidate["announcement"]["url"] = "https://example.invalid/unapproved"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            errors = validator.validate_repository(root)
        self.assertTrue(any("stored source URL failed the canonical HTTPS policy" in error for error in errors))

    def test_generated_sample_markers_are_required(self):
        directory, root = self.make_repository()
        with directory:
            page = root / "site/index.html"
            source = page.read_text(encoding="utf-8").replace(slack_sample.START_MARKER, "", 1)
            page.write_text(source, encoding="utf-8")
            errors = validator.validate_repository(root)
        self.assertTrue(any("one complete generated Slack sample marker pair" in error for error in errors))

    def test_slack_sample_refuses_an_unsupported_block_shape(self):
        payload = {"text": "fallback", "mrkdwn": False, "blocks": [{"type": "divider"}]}
        with self.assertRaisesRegex(slack_sample.SampleGenerationError, "unsupported type"):
            slack_sample.project_payload(payload)

    def test_slack_sample_refuses_an_unsupported_text_field(self):
        payload = {
            "text": "fallback",
            "mrkdwn": False,
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "plain_text", "text": "sample", "emoji": False, "verbatim": True},
                }
            ],
        }
        with self.assertRaisesRegex(slack_sample.SampleGenerationError, "exact supported plain_text shape"):
            slack_sample.project_payload(payload)

    def test_slack_sample_escapes_renderer_text(self):
        payload = slack_sample.canonical_payload(ROOT)
        mutated = copy.deepcopy(payload)
        mutated["blocks"][1]["text"]["text"] = "<script>alert('sample')</script>"
        projected = slack_sample.project_payload(mutated)
        self.assertNotIn("<script>", projected)
        self.assertIn("&lt;script&gt;", projected)

    def test_embedded_mermaid_must_match_committed_source(self):
        directory, root = self.make_repository()
        with directory:
            source = root / "site/architecture.mmd"
            source.write_text(f"{source.read_text(encoding='utf-8')}\n%% changed\n", encoding="utf-8")
            errors = validator.validate_repository(root)
        self.assertTrue(any("embedded Mermaid source differs" in error for error in errors))

    def test_readme_cannot_take_back_the_mermaid_diagram(self):
        directory, root = self.make_repository()
        with directory:
            readme = root / "README.md"
            readme.write_text(
                f"{readme.read_text(encoding='utf-8')}\n```mermaid\nflowchart LR\n```\n", encoding="utf-8"
            )
            errors = validator.validate_repository(root)
        self.assertTrue(any("Mermaid architecture belongs" in error for error in errors))

    def test_broken_local_site_reference_is_rejected(self):
        directory, root = self.make_repository()
        with directory:
            page = root / "site/index.html"
            text = page.read_text(encoding="utf-8").replace("./architecture.mmd", "./missing.mmd", 1)
            page.write_text(text, encoding="utf-8")
            errors = validator.validate_repository(root)
        self.assertTrue(any("target does not exist" in error for error in errors))

    def test_public_sources_and_supporting_site_files_require_page_update(self):
        watched_paths = (
            "docs/GOAL.md",
            "docs/architecture/specification/01-overview.md",
            "docs/adr/017-public-feed-only-product-scope.md",
            "schemas/config.schema.json",
            "examples/config.yaml",
            "site/architecture.mmd",
            "site/compact-theme.css",
            "site/site.js",
        )
        for path in watched_paths:
            with self.subTest(path=path):
                errors = validator.validate_site_sync([path])
                self.assertTrue(any("public architecture content is stale" in error for error in errors))

    def test_page_update_satisfies_site_content_sync(self):
        changed = ["docs/GOAL.md", "site/architecture.mmd", "site/index.html"]
        self.assertEqual(validator.validate_site_sync(changed), [])

    def test_package_retirement_status_matches_the_implemented_tool(self):
        page = (ROOT / "site/index.html").read_text(encoding="utf-8")
        self.assertIn(
            "A controlled application-package retirement preview and, when naturally eligible packages exist, "
            "a separately authorized apply with retained-set verification",
            page,
        )
        self.assertNotIn("packages accumulate until it exists", page)

    def test_unrelated_change_does_not_require_page_update(self):
        self.assertEqual(validator.validate_site_sync(["tests/test_validate_config.py"]), [])

    def test_pages_workflow_uses_immutable_actions_and_minimum_permissions(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["deploy"]["steps"]
        workflow_pins.assert_pinned(steps[0]["uses"], "actions/checkout")
        workflow_pins.assert_pinned(steps[2]["uses"], "actions/configure-pages")
        workflow_pins.assert_pinned(steps[3]["uses"], "actions/upload-pages-artifact")
        workflow_pins.assert_pinned(steps[4]["uses"], "actions/deploy-pages")
        self.assertEqual(workflow["permissions"], {"contents": "read", "pages": "write", "id-token": "write"})
        self.assertEqual(steps[3]["with"]["path"], "site")

    def test_quality_workflow_checks_page_sync_against_the_change_base(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["validate"]["steps"]
        checkout = next(step for step in steps if step.get("name") == "Check out repository")
        site_sync = next(step for step in steps if step.get("name") == "Check public page content sync")
        self.assertEqual(checkout["with"]["fetch-depth"], 0)
        self.assertIn("scripts/validate_site.py --base", site_sync["run"])


if __name__ == "__main__":
    unittest.main()
