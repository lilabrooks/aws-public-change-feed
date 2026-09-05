import copy
import hashlib
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
import stamp_drawio_export as drawio_export  # noqa: E402
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

    def test_drawio_source_change_makes_the_svg_export_stale(self):
        directory, root = self.make_repository()
        with directory:
            source = root / "site/architecture.drawio"
            source.write_text(
                source.read_text(encoding="utf-8").replace('agent="Codex"', 'agent="Changed"'), encoding="utf-8"
            )
            errors = validator.validate_repository(root)
        self.assertTrue(any("export is stale" in error for error in errors))

    def test_drawio_export_stamp_binds_the_exact_source(self):
        directory, root = self.make_repository()
        with directory:
            source = root / "site/architecture.drawio"
            export = root / "site/architecture.svg"
            source.write_text(
                source.read_text(encoding="utf-8").replace('agent="Codex"', 'agent="Changed"'), encoding="utf-8"
            )
            digest = drawio_export.stamp_export(source, export)
            self.assertEqual(digest, hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(validator.validate_drawio_artifacts(root), [])

    def test_drawio_source_requires_the_durable_delivery_edge(self):
        directory, root = self.make_repository()
        with directory:
            source = root / "site/architecture.drawio"
            source.write_text(
                source.read_text(encoding="utf-8").replace(
                    'source="outbox" target="dispatcher"', 'source="outbox" target="worker"', 1
                ),
                encoding="utf-8",
            )
            errors = validator.validate_repository(root)
        self.assertTrue(any("e_outbox_dispatcher must connect outbox to dispatcher" in error for error in errors))

    def test_drawio_source_requires_the_aws_service_icons(self):
        directory, root = self.make_repository()
        with directory:
            source = root / "site/architecture.drawio"
            source.write_text(
                source.read_text(encoding="utf-8").replace(
                    "resIcon=mxgraph.aws4.cloudwatch", "resIcon=mxgraph.aws4.lambda", 1
                ),
                encoding="utf-8",
            )
            errors = validator.validate_repository(root)
        self.assertTrue(any("icon_operations_cloudwatch must use mxgraph.aws4.cloudwatch" in error for error in errors))

    def test_readme_cannot_take_back_the_architecture_diagram(self):
        directory, root = self.make_repository()
        with directory:
            readme = root / "README.md"
            readme.write_text(
                f"{readme.read_text(encoding='utf-8')}\n```mermaid\nflowchart LR\n```\n", encoding="utf-8"
            )
            errors = validator.validate_repository(root)
        self.assertTrue(any("architecture diagrams use the committed draw.io source" in error for error in errors))

    def test_page_uses_the_static_drawio_export_without_a_mermaid_runtime(self):
        page = (ROOT / "site/index.html").read_text(encoding="utf-8")
        self.assertIn('src="./architecture.svg"', page)
        self.assertIn('href="./architecture.drawio"', page)
        self.assertNotIn("mermaid", page.casefold())
        self.assertNotIn("site.js", page)

    def test_broken_local_site_reference_is_rejected(self):
        directory, root = self.make_repository()
        with directory:
            page = root / "site/index.html"
            text = page.read_text(encoding="utf-8").replace("./architecture.drawio", "./missing.drawio", 1)
            page.write_text(text, encoding="utf-8")
            errors = validator.validate_repository(root)
        self.assertTrue(any("target does not exist" in error for error in errors))

    def test_changed_mvp_media_is_rejected_by_its_hash_manifest(self):
        directory, root = self.make_repository()
        with directory:
            poster = root / validator.MVP_POSTER_PATH
            poster.write_bytes(poster.read_bytes() + b"changed")
            errors = validator.validate_repository(root)
        self.assertTrue(any("digest mismatch" in error and poster.name in error for error in errors))

    def test_mvp_video_requires_metadata_only_preload(self):
        directory, root = self.make_repository()
        with directory:
            page = root / "site/index.html"
            text = page.read_text(encoding="utf-8").replace('preload="metadata"', 'preload="auto"', 1)
            page.write_text(text, encoding="utf-8")
            errors = validator.validate_repository(root)
        self.assertTrue(any("must preload metadata only" in error for error in errors))

    def test_mvp_release_video_digest_is_bound_to_the_reviewed_file(self):
        directory, root = self.make_repository()
        with directory:
            hashes = root / validator.MVP_HASHES_PATH
            text = hashes.read_text(encoding="utf-8").replace(validator.MVP_VIDEO_SHA256, "0" * 64, 1)
            hashes.write_text(text, encoding="utf-8")
            errors = validator.validate_repository(root)
        self.assertTrue(any("release video digest does not match" in error for error in errors))

    def test_mvp_web_video_requires_an_iso_media_container(self):
        directory, root = self.make_repository()
        with directory:
            web_video = root / validator.MVP_WEB_VIDEO_PATH
            web_video.write_bytes(b"not a video")
            errors = validator.validate_repository(root)
        self.assertTrue(any("web video must be an ISO media file" in error for error in errors))

    def test_public_sources_and_supporting_site_files_require_page_update(self):
        watched_paths = (
            "docs/GOAL.md",
            "docs/architecture/specification/01-overview.md",
            "docs/adr/017-public-feed-only-product-scope.md",
            "schemas/config.schema.json",
            "examples/config.yaml",
            "site/architecture.drawio",
            "site/architecture.svg",
            "site/compact-theme.css",
        )
        for path in watched_paths:
            with self.subTest(path=path):
                errors = validator.validate_site_sync([path])
                self.assertTrue(any("public architecture content is stale" in error for error in errors))

    def test_page_update_satisfies_site_content_sync(self):
        changed = ["docs/GOAL.md", "site/architecture.drawio", "site/architecture.svg", "site/index.html"]
        self.assertEqual(validator.validate_site_sync(changed), [])

    def test_retirement_status_matches_the_implemented_tools(self):
        page = (ROOT / "site/index.html").read_text(encoding="utf-8")
        self.assertIn(
            "operator controls for retained-source replay, delivery replay, DLQ movement, package retirement, and configuration-release retirement",
            page,
        )
        self.assertNotIn("packages accumulate until it exists", page)

    def test_m2_status_matches_the_completed_milestone(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        goal = (ROOT / "docs/GOAL.md").read_text(encoding="utf-8")
        walkthrough = (ROOT / "docs/evidence/mvp-walkthrough.md").read_text(encoding="utf-8")
        page = (ROOT / "site/index.html").read_text(encoding="utf-8")

        self.assertIn("milestone/3) | Closed |", readme)
        self.assertIn("D0, M1, and M2 are closed. M3 is open.", page)
        self.assertIn("M2 · Closed", page)
        self.assertIn("76 announcement rows and 88 response-page rows", goal)
        self.assertIn("Production readiness remains open under M3.", walkthrough)
        for stale_claim in (
            "Remaining work will make each source-state load",
            "Recovery fixes will replace",
            "M2 is open",
            "M2 · Open",
            "under M2 and M3",
            "built locally but have not yet run against the table",
        ):
            with self.subTest(stale_claim=stale_claim):
                self.assertNotIn(stale_claim, "\n".join((readme, goal, walkthrough, page)))

    def test_m3_policy_decision_matches_the_evidence_record(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        goal = (ROOT / "docs/GOAL.md").read_text(encoding="utf-8")
        evidence = (ROOT / "docs/evidence/production-policy.md").read_text(encoding="utf-8")
        page = (ROOT / "site/index.html").read_text(encoding="utf-8")

        self.assertIn("selected the current 4-feed, 3-service, 4-risk-rule policy unchanged", readme)
        self.assertIn("selected the current 4-feed, 3-service, 4-risk-rule policy unchanged", goal)
        self.assertIn("Owner decision: Accepted on 2026-09-01.", evidence)
        self.assertIn("6 of 12 service and risk-type pairs carrying no historical positive", page)
        self.assertNotIn("will choose the production feed and matching policy", readme)
        self.assertNotIn("It will choose the production feed and matching policy", page)

    def test_source_state_lifecycle_matches_the_accepted_decision(self):
        page = (ROOT / "site/index.html").read_text(encoding="utf-8")
        self.assertIn("Twenty-three accepted ADRs", page)
        self.assertIn("Active feed checkpoints do not expire", page)
        self.assertIn("docs/adr/025-source-state-and-response-page-retirement.md", page)
        self.assertIn("A separate CloudTrail-only role captures digest-bound provider evidence", page)
        self.assertIn("docs/adr/028-separate-cloudtrail-evidence-for-dynamodb-restore-identity.md", page)

    def test_delivery_unknown_reassessment_matches_the_accepted_decision(self):
        decision = " ".join(
            (ROOT / "docs/adr/007-central-slack-delivery-queue-and-worker.md").read_text(encoding="utf-8").split()
        )
        goal = " ".join((ROOT / "docs/GOAL.md").read_text(encoding="utf-8").split())
        page = " ".join((ROOT / "site/index.html").read_text(encoding="utf-8").split())

        self.assertIn("## Revision: expired sending remains an unknown outcome", decision)
        self.assertIn("- Accepted: 2026-08-31", decision)
        self.assertIn("never automatically retries that record", decision)
        self.assertIn("it did not prove that a naturally expired network attempt sent nothing", page)
        self.assertIn("an inconclusive search leaves the record unchanged", page)
        self.assertIn("The 12-message public-feed cohort reached durable `posted`", goal)
        self.assertNotIn("The recovery reconciler remains undeployed", goal)
        self.assertNotIn("ADR-016 still requires an operator-confirmed test-notification receipt", goal)

    def test_unrelated_change_does_not_require_page_update(self):
        self.assertEqual(validator.validate_site_sync(["tests/test_validate_config.py"]), [])

    def test_pages_workflow_uses_immutable_actions_and_minimum_permissions(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8"))
        self.assertEqual(workflow["jobs"]["deploy"]["timeout-minutes"], 10)
        steps = workflow["jobs"]["deploy"]["steps"]
        step_names = [step["name"] for step in steps]
        steps_by_name = {step["name"]: step for step in steps}
        workflow_pins.assert_pinned(steps_by_name["Check out repository"]["uses"], "actions/checkout")
        workflow_pins.assert_pinned(steps_by_name["Set up Python"]["uses"], "actions/setup-python")
        workflow_pins.assert_pinned(steps_by_name["Configure GitHub Pages"]["uses"], "actions/configure-pages")
        workflow_pins.assert_pinned(steps_by_name["Upload public page"]["uses"], "actions/upload-pages-artifact")
        workflow_pins.assert_pinned(steps_by_name["Deploy public page"]["uses"], "actions/deploy-pages")
        self.assertEqual(workflow["permissions"], {"contents": "read", "pages": "write", "id-token": "write"})
        self.assertEqual(
            steps_by_name["Set up Python"]["with"],
            {"python-version": "3.12", "cache": "pip", "cache-dependency-path": "requirements-lambda.txt"},
        )
        self.assertEqual(
            steps_by_name["Install runtime dependencies"]["run"],
            "python -m pip install --no-deps --requirement requirements-lambda.txt",
        )
        self.assertLess(
            step_names.index("Install runtime dependencies"),
            step_names.index("Validate public page"),
        )
        self.assertEqual(
            steps_by_name["Validate public page"]["run"],
            "python scripts/validate_site.py",
        )
        self.assertEqual(steps_by_name["Upload public page"]["with"]["path"], "site")
        watched_paths = workflow["on"]["push"]["paths"]
        self.assertEqual(
            set(watched_paths),
            {
                "site/**",
                "scripts/generate_slack_sample.py",
                "scripts/validate_site.py",
                "requirements-lambda.txt",
                ".github/workflows/pages.yml",
            },
        )

    def test_quality_workflow_checks_page_sync_against_the_change_base(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["validate"]["steps"]
        checkout = next(step for step in steps if step.get("name") == "Check out repository")
        site_sync = next(step for step in steps if step.get("name") == "Check public page content sync")
        self.assertEqual(checkout["with"]["fetch-depth"], 0)
        self.assertIn("scripts/validate_site.py --base", site_sync["run"])


if __name__ == "__main__":
    unittest.main()
