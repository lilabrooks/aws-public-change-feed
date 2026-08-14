import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import workflow_pins
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_references as validator  # noqa: E402

# A fixed clock keeps the committed-reference assertions deterministic. Move it
# forward when a document is verified on a later date; the 180-day warning and
# 365-day maximum leave ample room before older markers need re-verification.
AS_OF = date(2026, 8, 14)
VALID_LYCHEE_CONFIG = (ROOT / "lychee.toml").read_text(encoding="utf-8")

# Fixture dates are derived from AS_OF so each one states the condition it
# exercises rather than a literal that only satisfies it under today's clock.
# Moving AS_OF forward moves them with it.
WARNING_AGE_MARKER = (AS_OF - timedelta(days=203)).isoformat()
MAXIMUM_AGE_MARKER = (AS_OF - timedelta(days=389)).isoformat()
# The validator treats an exclusion as expired only once its expiry falls
# strictly before as_of, so AS_OF itself is still current.
UNEXPIRED_EXCLUSION = (AS_OF + timedelta(days=28)).isoformat()
EXPIRED_EXCLUSION = (AS_OF - timedelta(days=1)).isoformat()


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
        # Derived from AS_OF so advancing the fixed clock does not also require
        # editing this fixture. A hardcoded marker here is not a silent hazard:
        # once AS_OF passes it, the date is no longer in the future, the
        # validator reports nothing, and this assertion fails. It fails loudly,
        # just at a moment unrelated to the change that caused it.
        tomorrow = AS_OF + timedelta(days=1)
        markdown = f"# Test\n\nhttps://example.com\n\nReferences verified: {tomorrow.isoformat()}.\n"
        directory, root = self.make_repository(markdown)
        with directory:
            errors, _, _, _ = self.validate(root)
        self.assertTrue(any("in the future" in error for error in errors))

    def test_reference_age_warns_after_180_days(self):
        markdown = f"# Test\n\nhttps://example.com\n\nReferences verified: {WARNING_AGE_MARKER}.\n"
        directory, root = self.make_repository(markdown)
        with directory:
            errors, warnings, _, _ = self.validate(root)
        self.assertEqual(errors, [])
        self.assertTrue(any("review warning after 180" in warning for warning in warnings))

    def test_reference_age_fails_after_365_days(self):
        markdown = f"# Test\n\nhttps://example.com\n\nReferences verified: {MAXIMUM_AGE_MARKER}.\n"
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
            "missing reason": f"# Expires: {UNEXPIRED_EXCLUSION}\n^https://example\\.com$\n",
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
            "expired": f"# Reason: blocked\n# Expires: {EXPIRED_EXCLUSION}\n^https://example\\.com$\n",
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
            "# Reason: host rejects identified automated clients\n"
            f"# Expires: {UNEXPIRED_EXCLUSION}\n^https://example\\.com$\n"
        )
        directory, root = self.make_repository("# Test\n", exclusions)
        with directory:
            errors, _, _, _ = self.validate(root)
        self.assertEqual(errors, [])

    def test_lychee_exclusion_metadata_must_be_adjacent_and_ordered(self):
        cases = {
            "blank before pattern": (
                f"# Reason: blocked\n# Expires: {UNEXPIRED_EXCLUSION}\n\n^https://example\\.com$\n"
            ),
            "comment before pattern": (
                f"# Reason: blocked\n# Expires: {UNEXPIRED_EXCLUSION}\n# Temporary note\n^https://example\\.com$\n"
            ),
            "reversed metadata": (f"# Expires: {UNEXPIRED_EXCLUSION}\n# Reason: blocked\n^https://example\\.com$\n"),
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
        workflow_pins.assert_pinned(lychee_step["uses"], "lycheeverse/lychee-action")
        self.assertEqual(lychee_step["with"]["lycheeVersion"], "v0.24.2")

        restore_step = next(step for step in steps if step.get("name") == "Restore link-check cache")
        save_step = next(step for step in steps if step.get("name") == "Save link-check cache")
        restore_sha = workflow_pins.assert_pinned(restore_step["uses"], "actions/cache/restore")
        save_sha = workflow_pins.assert_pinned(save_step["uses"], "actions/cache/save")
        self.assertEqual(restore_sha, save_sha, "cache restore and save must come from one release")

    def test_quality_workflow_runs_pinned_python_312_checks(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["validate"]["steps"]
        checkout = next(step for step in steps if step.get("name") == "Check out repository")
        setup = next(step for step in steps if step.get("name") == "Set up Python")
        checks = next(step for step in steps if step.get("name") == "Run repository checks")
        workflow_pins.assert_pinned(checkout["uses"], "actions/checkout")
        workflow_pins.assert_pinned(setup["uses"], "actions/setup-python")
        self.assertEqual(setup["with"]["python-version"], "3.12")
        self.assertEqual(checks["run"], "make check PYTHON=python REQUIRE_TERRAFORM=1")

    def test_quality_workflow_installs_terraform_before_the_checks(self):
        """The local default may skip Terraform, while CI must require it.

        This binds both CI jobs to installed Terraform versions, keeps the
        workflow minimum at the exact accepted floor, and makes CI fail closed.
        """

        workflow = yaml.safe_load((ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["validate"]["steps"]
        names = [step.get("name") for step in steps]
        setup = next(step for step in steps if step.get("name") == "Set up Terraform")
        workflow_pins.assert_pinned(setup["uses"], "hashicorp/setup-terraform")
        self.assertLess(names.index("Set up Terraform"), names.index("Run repository checks"))
        self.assertFalse(setup["with"]["terraform_wrapper"], "the Makefile reads exit codes directly")

        minimum_version = "1.10.0"
        constraint = ">= 1.10.0, < 2.0.0"
        for root in ("infra/bootstrap", "infra/central"):
            with self.subTest(root=root):
                versions = (ROOT / root / "versions.tf").read_text(encoding="utf-8")
                required_version_lines = [
                    line.strip() for line in versions.splitlines() if line.strip().startswith("required_version")
                ]
                self.assertEqual(required_version_lines, [f'required_version = "{constraint}"'])
        self.assertEqual(setup["with"]["terraform_version"], "1.15.8")

        minimum_steps = workflow["jobs"]["terraform-minimum"]["steps"]
        minimum_setup = next(step for step in minimum_steps if step.get("name") == "Set up minimum Terraform")
        minimum_version_check = next(
            step for step in minimum_steps if step.get("name") == "Confirm minimum Terraform version"
        )
        minimum_check = next(
            step for step in minimum_steps if step.get("name") == "Validate Terraform roots at the minimum version"
        )
        workflow_pins.assert_pinned(minimum_setup["uses"], "hashicorp/setup-terraform")
        self.assertEqual(minimum_setup["with"]["terraform_version"], minimum_version)
        self.assertFalse(minimum_setup["with"]["terraform_wrapper"])
        self.assertEqual(
            minimum_version_check["run"].splitlines(),
            [
                "terraform version",
                f'test "$(terraform version | sed -n \'1p\')" = "Terraform v{minimum_version}"',
            ],
        )
        self.assertEqual(minimum_check["run"], "make terraform-check REQUIRE_TERRAFORM=1")

    def test_provider_lockfiles_cover_the_ci_and_local_platforms(self):
        """A lockfile locked only on the author's platform fails init on the runner."""

        for root in ("infra/bootstrap", "infra/central"):
            lockfile = (ROOT / root / ".terraform.lock.hcl").read_text(encoding="utf-8")
            self.assertEqual(
                lockfile.count('"h1:'),
                2,
                f"{root} must lock both linux_amd64 (CI) and darwin_arm64; "
                "run terraform providers lock -platform=linux_amd64 -platform=darwin_arm64",
            )


if __name__ == "__main__":
    unittest.main()
