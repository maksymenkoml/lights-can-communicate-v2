"""Run existing adapter contracts with runtime modules imported from the archive."""

import importlib
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = [
    "test_live_adapter",
    "test_power_owner",
    "test_power_owner_integration",
    "test_video_output",
    "test_hue_strip_proxy",
    "test_hue_entertainment_relay",
    "test_pilot_transport",
    "test_govee_segments",
    "test_adapter_lifecycle",
    "test_discovery_integration",
    "test_live_preview",
    "test_native_profile",
    "test_native_profile_integration",
    "test_stream_http",
    "test_stream_diagnostics",
]
RUNTIME = [
    "live_adapter",
    "smart_lighting",
    "desk_layout",
    "entertainment_stream",
    "govee_segments",
    "hue_entertainment_relay",
    "hue_strip_proxy",
    "pilot_transport",
    "power_owner",
    "video_output",
    "bridge_output",
    "smart_lighting_core.native_profile",
    "stream_http",
    "stream_diagnostics",
]


def main():
    if len(sys.argv) == 1:
        for mode in ("false", "true"):
            subprocess.run(
                [sys.executable, "-B", __file__, mode],
                check=True,
                env=dict(os.environ, PROBE_GOVEE_GRADIENT=mode),
            )
        return
    with TemporaryDirectory(prefix="tmp_core_archive_contract_") as folder:
        artifact = Path(folder) / "adapter.pyz"
        subprocess.run(
            [sys.executable, "-B", "-m", "smart_lighting_core.build", "--output", str(artifact)],
            check=True,
            cwd=ROOT,
        )
        sys.path.insert(0, str(artifact))
        for name in RUNTIME:
            module = importlib.import_module(name)
            if not module.__file__.startswith(str(artifact) + "/"):
                raise AssertionError("Contract test imported a module outside the archive: " + name)
        # Legacy video fixtures explicitly assume fourteen whole-color channels.
        # The active-profile contracts exercise the real eighteen-channel video
        # path with seven-zone targets and power gating, without adapting them.
        contracts = CONTRACTS + ["test_active_video_profile"]
        if sys.argv[1] == "true":
            contracts = [
                name for name in contracts if name not in {"test_video_output", "test_live_adapter"}
            ]
        suite = unittest.defaultTestLoader.loadTestsFromNames(contracts)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        if not result.wasSuccessful():
            raise SystemExit(1)
        print("Archive contracts:", sys.argv[1], "PASS", result.testsRun, flush=True)


if __name__ == "__main__":
    main()
