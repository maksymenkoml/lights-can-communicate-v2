"""Render the NAS container profile and validate it with the Docker Compose CLI.

Needs `docker compose`; creates no containers or networks. CI runs it on the
Linux runner; the unit tests cover the rendered values themselves.
"""

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lights_can_communicate_v2_core import services  # noqa: E402
from lights_can_communicate_v2_core.config import load_config  # noqa: E402
from lights_can_communicate_v2_core.dist import build_release  # noqa: E402


def main():
    with TemporaryDirectory(prefix="tmp_container_compose_") as folder:
        base = Path(folder).resolve()
        state, config_dir = base / "state", base / "config"
        state.mkdir()
        config_dir.mkdir()
        for name in ("targets.json", "adapter-token", "owner-token"):
            (state / name).write_text("{}" if name.endswith(".json") else name * 4)
        config_path = config_dir / "core.json"
        config_path.write_text(json.dumps({
            "version": 1,
            "state_dir": "../state",
            "adapter": {"targets_file": "../state/targets.json",
                        "token_file": "../state/adapter-token",
                        "power_owner_token_file": "../state/owner-token"},
            "gateway": {"lan_ip": "192.168.99.10", "mac": "02:00:00:00:00:0a"},
            "container": {
                "lan": {"parent": "eth0", "subnet": "192.168.99.0/24",
                        "router": "192.168.99.1", "adapter_ip": "192.168.99.11"},
                "studio_hosts": ["nas.local"],
            },
        }))
        release = base / "release"
        build_release(release)
        output = base / "services"
        services.render(load_config(config_path), config_path, release, output,
                        sys.executable, link=base / "current")
        compose = output / services.COMPOSE
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose), "config", "--format", "json"],
            capture_output=True, text=True, check=False)
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            return 1
        resolved = json.loads(result.stdout)
        expected = {"gateway", "adapter", "studio"}
        if set(resolved["services"]) != expected or set(resolved["networks"]) != {"lan", "core"}:
            print("Compose resolved an unexpected project", file=sys.stderr)
            return 1
        print(json.dumps({"compose": "valid", "services": sorted(expected)}))
        return 0


if __name__ == "__main__":
    sys.exit(main())
