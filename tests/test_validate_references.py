import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_references as validator  # noqa: E402

AS_OF = date(2026, 7, 13)
VALID_LYCHEE_CONFIG = (ROOT / "lychee.toml").read_text(encoding="utf-8")
LYCHEE_ACTION = "lycheeverse/lychee-action@e7477775783ea5526144ba13e8db5eec57747ce8"
CHECKOUT_ACTION = "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
SETUP_PYTHON_ACTION = "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405"


class ReferenceValidatorTests(unittest.TestCase):
    def make_repository(self, markdown: str, exclusions: str = "", lychee_config: str = VALID_LYCHEE_CONFIG):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        (root / "README.md").write_text(markdown, encoding="utf-8")
        (root / ".lycheeignore").write_text(exclusions, encoding="utf-8")
        (root / "lychee.toml").write_text(lychee_config, encoding="utf-8")
        return directory, root

    def validate(self, root: Path, as_of: date = AS_OF):
        return validator.validate_repository(root, as_of)

    def test_committed_references_pass_local_validation(self):
        errors, warnings, file_count, url_count = self.validate(ROOT)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertGreater(file_count, 0)
        self.assertGreater(url_count, 0)

    def test_external_url_without_verification_marker_is_rejected(self):
        directory, root = self.make_repository("# Test\n\nhttps://example.com\n")
        with directory:
            errors, _, _, _ = self.validate(root)
        self.assertTrue(any("external URLs require" in error for error in errors))

    def test_malformed_reference_date_is_rejected(self):
        cases = ("2026-99-99", "July-13-2026", "")
        for raw_date in cases:
            with self.subTest(raw_date=raw_date):
                markdown = f"# Test\n\nhttps://example.com\n\nReferences verified: {raw_date}\n"
                directory, root = self.make_repository(markdown)
                with directory:
                    errors, _, _, _ = self.validate(root)
                self.assertTrue(any("reference marker" in error or "invalid ISO date" in error for error in errors))

    def test_verbatim_reference_marker_and_link_are_ignored(self):
        markdown = (
            "# Test\n\n"
            "https://example.com\n\n"
            "`References verified: YYYY-MM-DD`\n\n"
            "```text\n"
            "[Example only](missing.md)\n"
            "References verified: 2026-07-13\n"
            "```\n"
        )
        directory, root = self.make_repository(markdown)
        with directory:
            errors, _, _, _ = self.validate(root)
        self.assertTrue(any("external URLs require" in error for error in errors))
        self.assertFalse(any("reference marker must contain" in error for error in errors))
        self.assertFalse(any("target does not exist" in error for error in errors))

    def test_future_reference_date_is_rejected(self):
        markdown = "# Test\n\nhttps://example.com\n\nReferences verified: 2026-07-14.\n"
        directory, root = self.make_repository(markdown)
        with directory:
            errors, _, _, _ = self.validate(root)
        self.assertTrue(any("in the future" in error for error in errors))

    def test_reference_age_warns_after_180_days(self):
        markdown = "# Test\n\nhttps://example.com\n\nReferences verified: 2026-01-13.\n"
        directory, root = self.make_repository(markdown)
        with directory:
            errors, warnings, _, _ = self.validate(root)
        self.assertEqual(errors, [])
        self.assertTrue(any("review warning after 180" in warning for warning in warnings))

    def test_reference_age_fails_after_365_days(self):
        markdown = "# Test\n\nhttps://example.com\n\nReferences verified: 2025-07-12.\n"
        directory, root = self.make_repository(markdown)
        with directory:
            errors, _, _, _ = self.validate(root)
        self.assertTrue(any("maximum 365" in error for error in errors))

    def test_broken_local_path_is_rejected(self):
        directory, root = self.make_repository("# Test\n\n[Missing](missing.md)\n")
        with directory:
            errors, _, _, _ = self.validate(root)
        self.assertTrue(any("target does not exist" in error for error in errors))

    def test_local_path_cannot_escape_repository(self):
        directory, root = self.make_repository("# Test\n\n[Outside](../outside.md)\n")
        with directory:
            errors, _, _, _ = self.validate(root)
        self.assertTrue(any("escapes the repository" in error for error in errors))

    def test_missing_markdown_anchor_is_rejected(self):
        markdown = "# Test\n\n[Missing section](#missing-section)\n"
        directory, root = self.make_repository(markdown)
        with directory:
            errors, _, _, _ = self.validate(root)
        self.assertTrue(any("anchor does not exist" in error for error in errors))

    def test_fragment_on_non_markdown_file_is_rejected(self):
        directory, root = self.make_repository("# Test\n\n[Invalid fragment](data.txt#section)\n")
        with directory:
            (root / "data.txt").write_text("section\n", encoding="utf-8")
            errors, _, _, _ = self.validate(root)
        self.assertTrue(any("fragment target is not a Markdown file" in error for error in errors))

    def test_existing_markdown_anchor_is_accepted(self):
        markdown = "# Test\n\n## Existing section\n\n[Section](#existing-section)\n"
        directory, root = self.make_repository(markdown)
        with directory:
            errors, _, _, _ = self.validate(root)
        self.assertEqual(errors, [])

    def test_broken_reference_style_local_links_are_rejected(self):
        markdown_by_style = {
            "full": "# Test\n\n[Missing][target]\n\n[target]: missing.md\n",
            "collapsed": "# Test\n\n[Target][]\n\n[target]: missing.md\n",
            "shortcut": "# Test\n\n[Target]\n\n[target]: missing.md\n",
        }
        for style, markdown in markdown_by_style.items():
            with self.subTest(style=style):
                directory, root = self.make_repository(markdown)
                with directory:
                    errors, _, _, _ = self.validate(root)
                self.assertTrue(any("target does not exist" in error for error in errors))

    def test_reference_style_link_to_setext_heading_is_accepted(self):
        markdown = "# Test\n\n[Existing section][target]\n\n[target]: target.md#existing-section\n"
        directory, root = self.make_repository(markdown)
        with directory:
            (root / "target.md").write_text("Existing section\n----------------\n", encoding="utf-8")
            errors, _, _, _ = self.validate(root)
        self.assertEqual(errors, [])

    def test_leading_horizontal_rule_does_not_hide_following_headings(self):
        markdown = "# Test\n\n[Existing section](target.md#existing-section)\n"
        directory, root = self.make_repository(markdown)
        with directory:
            (root / "target.md").write_text("---\n\n# Existing section\n", encoding="utf-8")
            errors, _, _, _ = self.validate(root)
        self.assertEqual(errors, [])

    def test_frontmatter_fields_do_not_create_markdown_anchors(self):
        markdown = "# Test\n\n[Metadata](target.md#status-draft)\n"
        directory, root = self.make_repository(markdown)
        with directory:
            (root / "target.md").write_text("---\nstatus: draft\n---\n\n# Target\n", encoding="utf-8")
            errors, _, _, _ = self.validate(root)
        self.assertTrue(any("anchor does not exist" in error for error in errors))

    def test_missing_lychee_exclusion_list_is_rejected(self):
        directory, root = self.make_repository("# Test\n")
        with directory:
            (root / ".lycheeignore").unlink()
            errors, _, _, _ = self.validate(root)
        self.assertTrue(any("missing documented Lychee exclusion list" in error for error in errors))

    def test_missing_or_invalid_lychee_configuration_is_rejected(self):
        cases = {
            "missing": None,
            "invalid": "cache = [\n",
        }
        for label, config in cases.items():
            with self.subTest(label=label):
                directory, root = self.make_repository("# Test\n", lychee_config=config or VALID_LYCHEE_CONFIG)
                with directory:
                    if config is None:
                        (root / "lychee.toml").unlink()
                    else:
                        (root / "lychee.toml").write_text(config, encoding="utf-8")
                    errors, _, _, _ = self.validate(root)
                expected = "missing Lychee configuration" if config is None else "invalid TOML"
                self.assertTrue(any(expected in error for error in errors))

    def test_each_lychee_policy_setting_rejects_a_changed_value(self):
        mutations = {
            "cache": ("cache = true", "cache = false"),
            "max_cache_age": ('max_cache_age = "1d"', 'max_cache_age = "2d"'),
            "cache_exclude_status": ('cache_exclude_status = "400.."', 'cache_exclude_status = "500.."'),
            "max_redirects": ("max_redirects = 10", "max_redirects = 0"),
            "max_retries": ("max_retries = 5", "max_retries = 0"),
            "max_concurrency": ("max_concurrency = 16", "max_concurrency = 1"),
            "timeout": ("timeout = 30", "timeout = 5"),
            "retry_wait_time": ("retry_wait_time = 3", "retry_wait_time = 0"),
            "method": ('method = "get"', 'method = "head"'),
            "require_https": ("require_https = true", "require_https = false"),
            "include_fragments": ('include_fragments = "anchor-only"', 'include_fragments = "all"'),
            "include_verbatim": ("include_verbatim = true", "include_verbatim = false"),
            "host_concurrency": ("host_concurrency = 2", "host_concurrency = 8"),
            "host_request_interval": ('host_request_interval = "250ms"', 'host_request_interval = "0ms"'),
            "extensions": ('extensions = ["md"]', 'extensions = ["md", "html"]'),
            "scheme": ('scheme = ["http", "https"]', 'scheme = ["https"]'),
            "exclude_all_private": ("exclude_all_private = true", "exclude_all_private = false"),
            "include_mail": ("include_mail = false", "include_mail = true"),
            "no_progress": ("no_progress = true", "no_progress = false"),
        }
        self.assertEqual(set(mutations), set(validator.LYCHEE_CONFIGURATION_POLICY))
        for key, (original, replacement) in mutations.items():
            with self.subTest(key=key):
                self.assertIn(original, VALID_LYCHEE_CONFIG)
                changed_config = VALID_LYCHEE_CONFIG.replace(original, replacement, 1)
                directory, root = self.make_repository("# Test\n", lychee_config=changed_config)
                with directory:
                    errors, _, _, _ = self.validate(root)
                self.assertTrue(any(f"setting {key} must be" in error for error in errors))

    def test_each_required_lychee_policy_setting_rejects_removal(self):
        lines = VALID_LYCHEE_CONFIG.splitlines(keepends=True)
        for key in validator.LYCHEE_CONFIGURATION_POLICY:
            with self.subTest(key=key):
                changed_config = "".join(line for line in lines if not line.startswith(f"{key} ="))
                directory, root = self.make_repository("# Test\n", lychee_config=changed_config)
                with directory:
                    errors, _, _, _ = self.validate(root)
                self.assertTrue(any(f"setting is missing: {key}" in error for error in errors))

    def test_unreviewed_lychee_setting_is_rejected(self):
        changed_config = f'{VALID_LYCHEE_CONFIG}\nexclude = ["https://example.com"]\n'
        directory, root = self.make_repository("# Test\n", lychee_config=changed_config)
        with directory:
            errors, _, _, _ = self.validate(root)
        self.assertTrue(any("unreviewed Lychee setting is not allowed: exclude" in error for error in errors))

    def test_lychee_exclusion_requires_reason_and_expiry(self):
        cases = {
            "missing both": "^https://example\\.com$\n",
            "missing reason": "# Expires: 2026-08-01\n^https://example\\.com$\n",
            "missing expiry": "# Reason: automated requests are blocked\n^https://example\\.com$\n",
        }
        for label, exclusions in cases.items():
            with self.subTest(label=label):
                directory, root = self.make_repository("# Test\n", exclusions)
                with directory:
                    errors, _, _, _ = self.validate(root)
                self.assertTrue(any("exclusion requires" in error for error in errors))

    def test_malformed_or_expired_lychee_exclusion_is_rejected(self):
        cases = {
            "malformed": "# Reason: blocked\n# Expires: next-week\n^https://example\\.com$\n",
            "noncanonical": "# Reason: blocked\n# Expires: 20260713\n^https://example\\.com$\n",
            "expired": "# Reason: blocked\n# Expires: 2026-07-12\n^https://example\\.com$\n",
        }
        for label, exclusions in cases.items():
            with self.subTest(label=label):
                directory, root = self.make_repository("# Test\n", exclusions)
                with directory:
                    errors, _, _, _ = self.validate(root)
                expected_by_label = {
                    "malformed": "invalid ISO date",
                    "noncanonical": "date must use YYYY-MM-DD",
                    "expired": "exclusion expired",
                }
                expected = expected_by_label[label]
                self.assertTrue(any(expected in error for error in errors))

    def test_current_documented_lychee_exclusion_is_accepted(self):
        exclusions = (
            "# Reason: host rejects identified automated clients\n# Expires: 2026-08-01\n^https://example\\.com$\n"
        )
        directory, root = self.make_repository("# Test\n", exclusions)
        with directory:
            errors, _, _, _ = self.validate(root)
        self.assertEqual(errors, [])

    def test_lychee_exclusion_metadata_must_be_adjacent_and_ordered(self):
        cases = {
            "blank before pattern": ("# Reason: blocked\n# Expires: 2026-08-01\n\n^https://example\\.com$\n"),
            "comment before pattern": (
                "# Reason: blocked\n# Expires: 2026-08-01\n# Temporary note\n^https://example\\.com$\n"
            ),
            "reversed metadata": ("# Expires: 2026-08-01\n# Reason: blocked\n^https://example\\.com$\n"),
        }
        for label, exclusions in cases.items():
            with self.subTest(label=label):
                directory, root = self.make_repository("# Test\n", exclusions)
                with directory:
                    errors, _, _, _ = self.validate(root)
                self.assertTrue(any("exclusion requires" in error for error in errors))

    def test_invalid_reference_age_policy_is_rejected(self):
        cases = ((-1, 365), (365, 365))
        for warning_days, maximum_days in cases:
            with self.subTest(warning_days=warning_days, maximum_days=maximum_days):
                directory, root = self.make_repository("# Test\n")
                with directory, self.assertRaisesRegex(ValueError, "reference age policy"):
                    validator.validate_repository(
                        root,
                        AS_OF,
                        warning_age_days=warning_days,
                        maximum_age_days=maximum_days,
                    )

    def test_reference_workflow_runs_local_validation_for_every_event(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/reference-links.yml").read_text(encoding="utf-8"))
        local_job = workflow["jobs"]["local-reference-validation"]
        self.assertNotIn("if", local_job)

    def test_reference_workflow_uses_compatible_immutable_lychee_versions(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/reference-links.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["online-reference-check"]["steps"]
        lychee_step = next(step for step in steps if step.get("name") == "Check external reference links")
        self.assertEqual(lychee_step["uses"], LYCHEE_ACTION)
        self.assertEqual(lychee_step["with"]["lycheeVersion"], "v0.24.2")

    def test_quality_workflow_runs_pinned_python_312_checks(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["validate"]["steps"]
        checkout = next(step for step in steps if step.get("name") == "Check out repository")
        setup = next(step for step in steps if step.get("name") == "Set up Python")
        checks = next(step for step in steps if step.get("name") == "Run repository checks")
        self.assertEqual(checkout["uses"], CHECKOUT_ACTION)
        self.assertEqual(setup["uses"], SETUP_PYTHON_ACTION)
        self.assertEqual(setup["with"]["python-version"], "3.12")
        self.assertEqual(checks["run"], "make check PYTHON=python")


if __name__ == "__main__":
    unittest.main()
