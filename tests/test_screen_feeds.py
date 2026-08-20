import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import screen_feeds as harness  # noqa: E402


class ScreenFeedPolicyTests(unittest.TestCase):
    def test_screener_defaults_to_the_dev_policy_relative_to_root(self):
        args = harness.parse_args([])
        self.assertEqual(args.config, Path("config/dev.yaml"))

    def test_selected_policy_supplies_feeds_services_and_rules(self):
        with (ROOT / "config/dev.yaml").open(encoding="utf-8") as handle:
            selected_configuration = yaml.safe_load(handle)
        selected_configuration["feeds"][0]["name"] = "selected-policy-feed"
        selected_configuration["services"]["eks"]["display_name"] = "Selected policy service"
        selected_configuration["risk_rules"][0]["priority"] = "medium"

        watcher = Mock()
        watcher.run.return_value = SimpleNamespace(outcomes=[], announcements=[], failed_feeds=[])
        selected_feeds = (object(),)
        selected_services = object()
        selected_rules = object()

        with tempfile.TemporaryDirectory() as directory:
            selected_root = Path(directory)
            (selected_root / "infra/central").mkdir(parents=True)
            (selected_root / "corpus").mkdir()
            shutil.copy2(
                ROOT / "infra/central/deployment.yaml",
                selected_root / "infra/central/deployment.yaml",
            )
            shutil.copy2(
                ROOT / "corpus/announcements.json",
                selected_root / "corpus/announcements.json",
            )
            selected = selected_root / "selected.yaml"
            selected.write_text(yaml.safe_dump(selected_configuration, sort_keys=False), encoding="utf-8")
            relative = selected.relative_to(selected_root)
            with (
                patch.object(harness, "load_feeds", return_value=selected_feeds) as load_feeds,
                patch.object(harness, "load_services", return_value=selected_services) as load_services,
                patch.object(harness, "load_risk_rules", return_value=selected_rules) as load_risk_rules,
                patch.object(harness, "FeedFetcher"),
                patch.object(harness, "FeedWatcher", return_value=watcher),
            ):
                with redirect_stdout(io.StringIO()):
                    result = harness.main(["--root", str(selected_root), "--config", str(relative)])

        self.assertEqual(result, 0)
        load_feeds.assert_called_once_with(selected_configuration)
        load_services.assert_called_once_with(selected_configuration)
        load_risk_rules.assert_called_once_with(selected_configuration)
        watcher.run.assert_called_once_with(list(selected_feeds))


if __name__ == "__main__":
    unittest.main()
