"""Build light registry files for tests from the legacy targets and profile shapes."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from lights_can_communicate_v2_core.light_registry import REGISTRY_FILE, migrate


def write_registry(state_dir, targets, profile=None):
    """Write ``lights.json`` in ``state_dir`` and return the registry.

    ``targets`` is the legacy target list and ``profile`` an optional native Hue
    profile dictionary; both are used the way the one-time migration reads them.
    """
    with TemporaryDirectory(prefix="tmp_registry_files_") as folder:
        root = Path(folder)
        (root / "targets.json").write_text(json.dumps({"targets": targets}))
        profile_file = None
        if profile is not None:
            profile_file = root / "native.json"
            profile_file.write_text(json.dumps(profile))
        registry = migrate(root / "targets.json", profile_file)
    registry.save(Path(state_dir) / REGISTRY_FILE)
    return registry
