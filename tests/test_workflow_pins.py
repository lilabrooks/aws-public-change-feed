import unittest
from collections import defaultdict

import workflow_pins


class WorkflowPinTests(unittest.TestCase):
    def test_every_workflow_declares_at_least_one_action(self):
        paths = workflow_pins.workflow_paths()
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(workflow=path.name):
                self.assertTrue(workflow_pins.uses_entries(path))

    def test_every_action_is_pinned_to_a_full_commit_sha(self):
        for path in workflow_pins.workflow_paths():
            for number, uses, _ in workflow_pins.uses_entries(path):
                with self.subTest(workflow=path.name, line=number):
                    match = workflow_pins.PINNED_USES.fullmatch(uses)
                    self.assertIsNotNone(match, f"{uses!r} must pin a full 40-character commit SHA, not a tag")

    def test_every_pinned_action_carries_a_readable_version_comment(self):
        for path in workflow_pins.workflow_paths():
            for number, uses, comment in workflow_pins.uses_entries(path):
                with self.subTest(workflow=path.name, line=number):
                    self.assertIsNotNone(comment, f"{uses!r} must record the pinned version in a trailing comment")
                    assert comment is not None
                    self.assertRegex(comment, workflow_pins.VERSION_COMMENT)

    def test_an_action_used_by_several_workflows_is_pinned_to_one_commit(self):
        shas_by_action: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for path in workflow_pins.workflow_paths():
            for number, uses, _ in workflow_pins.uses_entries(path):
                match = workflow_pins.PINNED_USES.fullmatch(uses)
                if match is not None:
                    shas_by_action[match["action"]][match["sha"]].append(f"{path.name}:{number}")
        for action, locations_by_sha in shas_by_action.items():
            with self.subTest(action=action):
                self.assertEqual(
                    len(locations_by_sha), 1, f"{action} is pinned inconsistently: {dict(locations_by_sha)}"
                )


if __name__ == "__main__":
    unittest.main()
