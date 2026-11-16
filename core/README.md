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


## Discovery and real layout placements

The local `0.1.0.dev2` adapter exposes authenticated `GET /discovery`. Its catalog
observes the existing sole Govee reply listener and reads bridge/device/light
metadata from the already paired Hue Bridge. It creates no extra Govee socket,
status polling target or hardware writer. Govee LAN Control is required for LAN
observations. Public catalog responses omit physical IDs, addresses and keys;
opaque placement IDs remain stable across address/name changes.

The setup UI's add menu now selects real catalog lights, derives their shape and
zones, snaps them to the five-centimeter grid and autosaves canonical bindings.
Already placed, unsupported, stale and ambiguous observations cannot be added as
new bindings. Existing saved bindings remain editable offline. Hue catalog
`online` means fresh Bridge inventory, not physical Zigbee reachability. New
bindings configure placement/calibration only; the six current hardware output
roles remain unchanged until the next topology/enrollment component.

Live activation found 12 elements (7 supported lights, 6 matched to existing
placements); an additional LCX005 strip was selectable. The existing layout bytes,
virtual Hue IDs, selected hardware and Play intent were preserved. Both Govee
were Off and remained Off with zero adapter color/brightness commands at sampled
checkpoints; the four already-On native Hue fixtures continued scene writes.
See `evidence/discovery-layout.json`. The owner requested opening the layout
himself, so API and automated DOM checks were used without opening his tab.


## Live output preview

`0.1.0.dev3` adds a passive shared recorder to the existing successful output
operations and appends bounded `preview` data to authenticated `/status`. It
records emitted Govee zones, accepted Hue scene PUTs and calibrated native RGB16
frames. These are command colors, not optical measurements or segment readback.
Scene/video transitions share the recorder; failed sends do not advance the
cache, and observer failures do not change transport results or sequencing.

The local gateway exposes normalized zone RGB and source-aware Sync state through
`/api/pilot`. Fresh Off/stale/unknown data masks glow. Retained idle receiver
sessions report waiting; stale former external streaming cannot latch foreign.
Static valid output stays visible while its native metadata remains fresh.
The UI paints existing SVG attributes and never saves or rebuilds the layout for
telemetry. It uses bounded visibility-aware polling, an independent freshness
guard, and neutral placeholders for unbound placements.

After the owner's explicit morning resume, the prepared artifact and gateway
were activated with the same output selection. All six initially-Off fixtures
remained Off; preview had no active colors. After normal On control, scene output,
active PS5 Sync and Stop-to-scene produced fresh colors on all six roles. The
owner's layout was not opened or changed. These are transport/API checks; optical
matching and the owner's visual acceptance of this preview remain separate.
See `evidence/live-preview-offline.json` for the historical overnight checks and
`evidence/live-preview-live.json` for morning results and the UDP publication limit.

## Native Hue selection profile

`0.1.0.dev4` supports optional `adapter.native_profile_file` in version-one service
configuration. The path resolves relative to the service configuration, requires
`hue_config_file`, and contains a strict version-one profile of the same four light
roles, native Entertainment area and twelve service/index mappings. The loader
rejects duplicate/unknown fields, invalid UUIDs, overlapping resources and changed
topology. Credentials stay in their existing separate files.

An explicit profile controls each client/proxy/relay instance; it overrides old
credential light/area hints without changing global constants. Resource membership,
channel order/index and resource types must match live metadata before stream
start. Legacy configuration without a profile retains its existing behavior.
This is still the fixed desk topology, not arbitrary device enrollment or a build
free of legacy identity defaults. Newly placed discovery items remain configuration
only until a separate enrollment component is implemented.

The owner's same-selection profile replaced dev3 locally after read-only metadata
parity. Live scene, PS5 Sync and Stop-to-scene checks passed on dev4; all six roles,
calibration, HomeKit identities, pairing and geometry were retained. The NAS was
not changed. The previous archive/configuration remain available for rollback.
See `evidence/native-hue-profile.json` for tests, live frame samples and limits.

## Stream recovery diagnostics

`0.1.0.dev5` reuses the authenticated bridge-to-adapter HTTP connection for
latest-frame delivery. Authentication is checked on every request; only fully
consumed stream requests opt into reuse. Non-stream routes and rejected requests
close their connections. Failed messages are not replayed; the next attempt uses
the latest queued frame, with the existing timestamp/sequence/session fences.

An established video output can reserve its last physical colors for up to two
seconds after its last accepted frame, without sending expired frames. During
that interval the UI reports waiting and scene writes remain paused. Explicit
Stop, shutdown, confirmed native Off or unsafe native state releases immediately;
longer gaps restore the scene. Native Off retains the blocked-session rule.

Authenticated adapter status now includes video transition counters and recent
sanitized events. Receiver status includes frame age, HTTP failure categories,
latency, successful-forward gaps, coalescing/expiry counts and connection counts.
Persistent journals in the existing state directory are
`adapter-stream-health.jsonl` and `bridge-stream-health.jsonl`. Each retains at
most 256 KiB plus one `.1` backup, mode `0600`, and 64 copied recent events in
memory. Only fixed state/reason labels and numeric metrics are recorded. No
credentials, frame payloads, exception text or device addresses are logged by
these journals. A journal failure increments `write_errors` without interrupting
output. Existing protocol request logs are separate.

On the Mac pilot, stop Sync Box before recreating the bridge container, wait for
the API/UDP guard, verify ingress, then start Box/Sync. A live Box sending into
the container startup window has coincided with a broken Docker Desktop UDP
publication; a running in-container guard alone is not proof of host ingress.
The control-datagram counter and actual DTLS frame counter are distinct checks.
This operational recovery is not an automatic Docker Desktop repair mechanism.
See `evidence/sync-transport-recovery.json` for the reproduced delivery failure,
sequential live observation windows, tests and measurement limits.
