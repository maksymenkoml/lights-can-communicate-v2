# Packaged adapter

First production-transition component: a reproducible executable archive of the
existing desk adapter. The Python standard library can execute this artifact
without a checkout or an installation step. This is not the complete NAS core or
a published generic device package. Native Hue light/area identities and channel
roles remain the pilot's pinned profile until the next component is extracted.

## Build and run

From the repository root:

```sh
python3 -B -m smart_lighting_core.build --output /new/path/smart-lighting-adapter.pyz
python3 -I -B /new/path/smart-lighting-adapter.pyz --version
python3 -I -B /new/path/smart-lighting-adapter.pyz --config /private/core.json --check
python3 -I -B /new/path/smart-lighting-adapter.pyz --config /private/core.json
```

Python 3.10+ is required. Native video also requires OpenSSL; preflight checks its
availability before starting the adapter. No pip/runtime library installation is
required. Keep `-I`: it excludes Python environment and working-directory import
injection. The archive contains the explicit public runtime allowlist and a
SHA256 source manifest, with fixed zip metadata. Existing output paths are never
overwritten. Build a new version alongside the installed artifact for rollback.

Use `config.example.json` as the service configuration structure. All paths,
including `state_dir`, resolve relative to the configuration file. Optional Hue
configuration fields can be omitted for Govee-only operation. Native video
requires both Hue configuration and `hue_play`. `bind` defaults to loopback;
select a reachable local address explicitly when the virtual bridge or existing
Homebridge coordinator runs on another host/container.

Referenced files retain the existing pilot formats. The two local token files
must already exist, contain distinct bounded hexadecimal credentials and have
owner-only permissions on POSIX. Preflight never generates credentials or
changes permissions. Supply existing files through deployment/migration, not
through a source checkout or an image layer. Configuration diagnostics never
print credential contents. The version-one launcher requires the existing power
coordinator credential for both selected Govee.

`--check` validates configuration, local dependencies and runtime imports without
starting sockets or writing state. Its success does not establish device
reachability, native power confirmation or an authenticated Homebridge owner
heartbeat. An active `state_dir/live-output-hold` is reported by check mode and
blocks actual startup. `desk-layout.json` is read from that same state directory.

## Compatibility and operation

The old `experiments/hue-bridge/live_adapter.py` command and flags still work.
Its state directory still defaults to the target file's parent, and its bind
address still defaults to all interfaces. The packaged launcher adds explicit
paths and reports `service.name`/`service.version` on the existing authenticated
`/status` route. Existing command, stream and power-owner routes are unchanged.
SIGTERM uses the original adapter's orderly KeyboardInterrupt/finalizer path.

Only one adapter may own the physical outputs and Govee reply port at a time.
Before a verified local cutover, record the exact old invocation and current
power state. Stop the old process, then start the checked artifact with the same
files, address/port and profile. If it fails, stop the new process before using
the recorded old invocation. No second writer should be started as a probe.
Do not move/recreate HomeKit or Hue pairing state for this component replacement.

The isolated process fixture requires the pinned test image, `--network none`,
no published ports/private mounts and `SMART_LIGHTING_ISOLATED_TESTS=1`. It starts
only synthetic Govee targets, reads real local HTTP status and delivers SIGTERM.
Default developer tests skip this opt-in lifecycle fixture. The pre-push hook also runs `python3 -B tests/check_core_archive.py`, exercising
existing protocol, power and video suites with archive imports in fourteen- and
eighteen-channel modes. The eighteen-channel run uses the dedicated active-profile video contracts;
legacy whole-color video fixtures remain in the fourteen-channel run. Both modes
exercise seven-zone packet encoding, power ownership and calibrated output.

References: [Python executable archives](https://docs.python.org/3/library/zipapp.html).


## Current local acceptance

The `0.1.0.dev1` artifact replaced the Mac adapter on 2026-11-15. The old
process exited before the new listener started. It runs from the archive under
isolated Python with the existing private files and state; no NAS plugin,
virtual Bridge identity or HomeKit resource was replaced.

Scene commands resumed, followed by real Sync Box frames and seven-zone targets
on both Govee. An old idle source session needed explicit virtual-area Stop and
a new Hue-app Start before fresh frames resumed. This is not seamless source
reconnection acceptance. Final Stop restored all four native Hue fixtures to
normal mode and six dynamic scene lights; all six already-On states were
preserved at the sampled checkpoints. See `evidence/local-cutover.json` for
counts and boundaries. New visual latency/smoothness acceptance was not recorded.
