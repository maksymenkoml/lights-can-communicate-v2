"""Missing native selection must fail before any physical or local side effect."""

import io
import json
import sys
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import nas_profile

from lights_can_communicate_v2_core import cli
from lights_can_communicate_v2_core.config import ConfigError, load_config

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import live_adapter as adapter  # noqa: E402
import test_native_profile_integration as integration  # noqa: E402
from hue_entertainment_relay import HueEntertainmentRelay  # noqa: E402
from hue_strip_proxy import HueStripProxy, PinnedHueClient  # noqa: E402
from registry_files import write_registry  # noqa: E402
from test_live_adapter import targets  # noqa: E402
from test_native_profile_integration import (  # noqa: E402
    hue_config,
    stream_config,
)


class ExplicitSelectionTests(unittest.TestCase):
    def test_missing_profile_is_rejected_by_each_native_output_constructor(self):
        # Valid legacy hints must never authorize a physical output on their own.
        legacy_hue = {**hue_config(), "light": "98250ec6-d7fd-4b47-ace6-ff449ea3066f"}
        legacy_stream = {**stream_config(), "area_id": "10c81079-56a6-45c4-8149-55185f703083"}
        for factory in (
            lambda: PinnedHueClient(legacy_hue),
            lambda: HueStripProxy(object(), "hue-monitor"),
            lambda: HueEntertainmentRelay(legacy_stream),
        ):
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

    def test_source_launcher_rejects_hue_without_profile_before_reading_token(self):
        with TemporaryDirectory(prefix="tmp_explicit_selection_") as folder:
            root = Path(folder)
            write_registry(root, targets())
            arguments = ["--registry", str(root / "lights.json"),
                         "--token-file", str(root / "missing-token"),
                         "--hue-config", str(root / "missing-hue.json")]
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                adapter.main(arguments)
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual([item.name for item in root.iterdir()], ["lights.json"])

    def test_service_config_without_profile_loads_and_prepare_rejects_it(self):
        with TemporaryDirectory(prefix="tmp_explicit_selection_") as folder:
            path = Path(folder) / "service.json"
            path.write_text(json.dumps({
                "version": 1, "state_dir": "state", "adapter": {
                    "targets_file": "targets.json", "token_file": "token",
                    "power_owner_token_file": "owner-token", "hue_config_file": "hue.json",
                },
                **nas_profile.sections(),
            }))
            config = load_config(path)
            (Path(folder) / "targets.json").write_text(json.dumps({"targets": targets()}))
            with patch.object(cli, "_credential", side_effect=AssertionError("Too late")):
                with self.assertRaises(ConfigError):
                    cli.prepare(config, write_registry=True)
            self.assertFalse(config.state_dir.exists())

    def test_prepare_rejects_programmatic_config_before_credentials_or_runtime(self):
        fixture = integration.ProfileConfigurationIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config = replace(fixture.config(), native_profile_file=None)
        with patch.object(cli, "_credential", side_effect=AssertionError("Too late")):
            with self.assertRaises(ConfigError):
                cli.prepare(config, write_registry=True)
        self.assertFalse(config.state_dir.exists())
