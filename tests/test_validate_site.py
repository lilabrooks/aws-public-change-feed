import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_site as validator  # noqa: E402

CHECKOUT_ACTION = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
CONFIGURE_PAGES_ACTION = "actions/configure-pages@45bfe0192ca1faeb007ade9deae92b16b8254a0d"
UPLOAD_PAGES_ACTION = "actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9"
DEPLOY_PAGES_ACTION = "actions/deploy-pages@cd2ce8fcbc39b97be8ca5fce6e763baed58fa128"


class SiteValidatorTests(unittest.TestCase):
    def make_repository(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        shutil.copytree(ROOT / "site", root / "site")
        shutil.copy2(ROOT / "README.md", root / "README.md")
        return directory, root

    def test_committed_site_passes_validation(self):
        self.assertEqual(validator.validate_repository(ROOT), [])

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

    def test_unrelated_change_does_not_require_page_update(self):
        self.assertEqual(validator.validate_site_sync(["tests/test_validate_config.py"]), [])

    def test_pages_workflow_uses_immutable_actions_and_minimum_permissions(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["deploy"]["steps"]
        self.assertEqual(steps[0]["uses"], CHECKOUT_ACTION)
        self.assertEqual(steps[2]["uses"], CONFIGURE_PAGES_ACTION)
        self.assertEqual(steps[3]["uses"], UPLOAD_PAGES_ACTION)
        self.assertEqual(steps[4]["uses"], DEPLOY_PAGES_ACTION)
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
