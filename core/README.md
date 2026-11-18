# Packaged adapter

First production-transition component: a reproducible executable archive of the
existing desk adapter. The Python standard library can execute this artifact
without a checkout or an installation step. A complete NAS service and arbitrary
device enrollment remain separate components. Physical native Hue
identities come only from an explicit profile. The four native roles and twelve
native channels remain fixed until a separate enrollment component is implemented.

## Build and run

From the repository root:

```sh
python3 -B -m lights_can_communicate_v2_core.build --output /new/path/lights-can-communicate-v2-adapter.pyz
python3 -I -B /new/path/lights-can-communicate-v2-adapter.pyz --version
python3 -I -B /new/path/lights-can-communicate-v2-adapter.pyz --config /private/core.json --check
python3 -I -B /new/path/lights-can-communicate-v2-adapter.pyz --config /private/core.json
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
requires Hue configuration, `hue_play` and an explicit native profile. Every Hue
configuration requires `adapter.native_profile_file`. Omit all Hue fields,
including the profile path, for Govee-only operation. `bind` defaults to loopback;
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

## Managed service release

A release directory holds everything the services run, without a checkout:

```sh
python3 -B -m lights_can_communicate_v2_core.dist --output /new/path/releases/0.1.0.devN
R=/new/path/releases/0.1.0.devN
python3 -I -B $R/lights-can-communicate-v2.pyz services --config /private/core.json \
  --release $R --output /new/path/services-0.1.0.devN
```

The release contains `lights-can-communicate-v2.pyz` (adapter and Setup Studio),
`gateway/` (the virtual bridge modules) and `manifest.json` (version and
SHA256 per file). `services` refuses a release of another version and writes
into a new directory only:

- `com.lights-can-communicate-v2.adapter.plist` and `com.lights-can-communicate-v2.setup.plist`:
  launchd agents that start at login and restart only after a non-zero exit.
  Logs go to `state_dir/logs`, which `services` creates because launchd does
  not create missing log directories.
- `gateway-compose.json` (only with a `gateway` section): the pinned diyHue
  image with `restart: unless-stopped`, read-only module mounts from the
  release, `state_dir` as its configuration directory and a health check.

Commands of the archive: `adapter` (default), `setup` (Setup Studio on
`setup.bind:setup.port`, default `127.0.0.1:19881`), `ready` (exit 0 once the
adapter `/status` and, if configured, the bridge `/api/config` answer locally;
prints a credential-free summary) and `services`. An active
`state_dir/live-output-hold` makes `adapter` exit 0 without output, so the
supervisor does not restart it in a loop.

Optional configuration sections:

```json
"setup": {"bind": "127.0.0.1", "port": 19881},
"gateway": {"lan_ip": "192.168.1.20", "mac": "02:00:00:00:00:01",
            "live_output": true, "hue_strip": true, "hue_play": true,
            "entertainment": true, "gradient": true}
```

`adapter.power_authority` selects who owns Govee power: `owner` (default, the
remote heartbeat of the Homebridge overlay) or `homekit` (this adapter executes
power for the `homebridge-lights-can-communicate-v2` plugin through `/homekit`;
`/power-owner` is then closed). See `homebridge/README.md`.

`gateway` also accepts `container_name`, `image` (a pinned `diyhue/core`
digest), `adapter_url` (default `http://host.docker.internal:<adapter port>`)
and `timezone`. With a `gateway` section the adapter `bind` must not be a
loopback address: the container reaches the host through its network. Rendering and readiness never load, start or stop services;
loading the agents and starting the compose project is a separate, owner
authorized activation. Design: `docs/design/2026-11-17-core-managed-service.md`.

## Compatibility and operation

The `experiments/hue-bridge/live_adapter.py` command retains its existing flags.
Hue output now requires `--native-profile` together with `--hue-config`.
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
no published ports/private mounts and `LIGHTS_CAN_COMMUNICATE_V2_ISOLATED_TESTS=1`. It starts
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

The initial `0.1.0.dev4` extraction introduced optional
`adapter.native_profile_file`. The current version-one service configuration
requires this field whenever `hue_config_file` is present. The path resolves relative to the service configuration, requires
`hue_config_file`, and contains a strict version-one profile of the same four light
roles, native Entertainment area and twelve service/index mappings. The loader
rejects duplicate/unknown fields, invalid UUIDs, overlapping resources and changed
topology. Credentials stay in their existing separate files.

An explicit profile controls each client/proxy/relay instance; it overrides old
credential light/area hints. The runtime contains no physical identity defaults.
Resource membership, channel order/index and resource types must match live metadata before stream
start. Configuration without a required profile now fails before initialization.
The native topology still has four light roles. Newly placed discovery items remain
configuration-only until a separate enrollment component is implemented.

The owner's same-selection profile replaced dev3 locally after read-only metadata
parity. Live scene, PS5 Sync and Stop-to-scene checks passed on dev4; all six roles,
calibration, HomeKit identities, pairing and geometry were retained. The NAS was
not changed. The previous archive/configuration remain available for rollback.
See `evidence/native-hue-profile.json` for tests, live frame samples and limits.

## Migrate a configuration without a native profile

Reuse the existing validated version-one profile when one is already installed.
Set `adapter.native_profile_file` to its path, relative to the service configuration.
For source invocations, pass that same file with `--native-profile`.

Older installations need a complete explicit profile before this artifact can run
Hue output. A credential file's `light` or `area_id` hint is not sufficient. Record
all four existing light UUIDs, the existing native area UUID and its twelve ordered
Entertainment service/index mappings. Use the version-one schema in
[`native-hue-profile.example.json`](native-hue-profile.example.json) as the structure.
Its synthetic UUIDs are placeholders. Never use them as a physical selection.
Validate the real selection against the existing installation before activation.

Keep the existing credentials, Govee targets, state directory and profile resource
identities. Keep pairing, HomeKit accessories/scenes and layout/calibration files.
Run the new archive with `--check` before an authorized cutover. Preflight reads
files without rewriting them or contacting lights. It does not verify live area
membership. A missing, partial or invalid profile blocks Hue startup.

Keep the previous artifact and configuration for rollback. Do not start a second
writer. This migration does not deploy the new artifact or change a running service.

## Installation, upgrades and rollback

Each release `manifest.json` declares a `compatibility` contract:
- configuration versions it reads;
- durable state schemas it writes;
- minimum Python and the pinned gateway image;
- the `/homekit` API version;
- the bundled Homebridge plugin tarball (`plugin/*.tgz`). The tarball is built
  deterministically in Python and needs no npm or network.

The plugin refuses a core whose `/homekit` API version differs from its
`CORE_API`. An installation directory holds `config/core.json`, `state/`,
`releases/<version>/`, a `current` symlink, `backups/` and the `install.json`
journal. Run every command from a release archive:

```sh
python3 -I -B RELEASE/lights-can-communicate-v2.pyz stage --release RELEASE --base BASE
python3 -I -B BASE/current/lights-can-communicate-v2.pyz activate VERSION --base BASE
python3 -I -B BASE/current/lights-can-communicate-v2.pyz rollback --base BASE
python3 -I -B BASE/current/lights-can-communicate-v2.pyz status --base BASE
```

`stage` does the following and never changes the active release:
1. Verifies every manifest hash.
2. Checks the configuration version and the Python version.
3. Runs the new archive's `adapter --check`.
4. Renders supervisor files whose paths go through `BASE/current`.

`activate` and `rollback` refuse while the adapter, Setup Studio or gateway
still answers locally. They snapshot durable state (without logs, request
logs or stream journals) and swap `current` atomically. They print the start
commands but never start, stop or restart services.

`rollback` restores the upgrade snapshot only when the two releases declare
different state schemas. Even then, the state it replaces is kept in the
rollback snapshot first. Each step is journaled and repeatable, so after an
interruption the next command finishes the transaction. The five newest
snapshots and the current upgrade point are kept. Activation and rollback of
the live installation still need the owner's approval; these commands never
publish packages.

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

### Source reconnection without a new Start

`openssl s_server -listen` serves one DTLS association and then only receives
datagrams from that peer. A source that vanished without `close_notify` and
handshakes again from a new port, without a new REST Start, previously reached
only the UDP guard, which discarded its ClientHello; the retained session
waited indefinitely until a phone Stop/Start. The guard now counts DTLS
ClientHello records it discards and remembers when the last one arrived. When
the current association has been idle for more than 0.5 seconds and a
ClientHello arrived during that pause, the receiver starts a second listener
beside it. The established association keeps receiving its own peer. The
replacement takes over only after it completes the PSK handshake and delivers
a valid frame; otherwise it is stopped after ten seconds, or as soon as the
established source resumes. An unauthenticated or stale ClientHello therefore
cannot disconnect a working source. Stop, a superseding Start and shutdown
end both listeners.

A source that vanishes and does not handshake again at all (a Sync Box reboot)
previously left the stream active indefinitely after its first frame; the Box
then saw its old session as active and never sent a new Start. As on a Hue
bridge, an established session now ends after ten seconds without data, the
stream becomes inactive and the source can start again. The bridge journal records `source_reconnected`,
`source_timeout` and `dtls_closed`, and receiver status counts `reconnects`.
`tests/test_receiver_reconnect.py`
reproduces the vanished-client case with real OpenSSL DTLS (Linux only).

## Light Studio composition and projection

`0.1.0.dev6` uses saved Light Studio placement and optional v3 `enabled:false`
membership for the six enrolled fixtures. Exclusion retains inventory identity,
position and calibration; picker rejoin restores that same row. The monitor anchor
remains editable independently of strip membership. Raised floor fixtures can
retain anchor heights up to 300 cm and fit the viewport. Other discovered bindings
remain configuration-only until explicit physical enrollment is implemented.

Sync captures a stable screen-perimeter palette using the existing virtual
resources/channels, then projects raw RGB16 samples locally onto the current
physical layout. Moving fixtures no longer changes capture topology. H6076's
seven emitting segments use a front-wall visibility estimate above desk height;
hidden segments receive black only in Sync. Play uses the default broad wall-wash
projection at least at desk height. Scenes continue using every zone of included
fixtures. This is a geometry approximation, not optical/ray-traced calibration.

Physical scene/video gates exclude removed fixtures. Native Sync reconciles a
subset of the same authorized four-light inventory in the same native area,
validates bridge readback and maps returned channel IDs by service/index. It does
not keep removed Play fixtures in an owned stream with black frames. Foreign
owners remain fenced. The private `native-location-cache.json` preserves full
service locations and equalization factors for rejoin after restart; atomic file
and directory sync complete before membership PUT. Cleanup can release an owned
stream even if cache persistence fails. No accessory IDs, credentials or NAS
installation are replaced.

Local scene exclusion/rejoin and inactive native-area subset/restore have passed
real API checks. The owner subsequently confirmed working output and a live
capture stream was observed. Source/archive/Linux checks and capture acceptance
are recorded in `evidence/layout-composition.json`. Rollback retains the prior artifact and
the original layout/native-area snapshots; restore those baseline memberships
before running a legacy artifact that does not understand composition selection.

## Soft desk visibility in Sync

`0.1.0.dev7` replaces v6's hard tabletop cutoff and unconditional Play wall-wash
lift. Light below the desk fades over 15 cm at the table edge. The band widens by
one centimeter for each centimeter of lateral distance outside the desk's sides;
desk width and saved fixture position determine it. Monitor offset does not move
the desk shadow. This is a front-wall approximation with a 1:1 spread, not a
measured optical model.

Govee uses the integrated visibility of each emitting segment and samples its
brightness-weighted centroid. Play uses its saved physical height: deeply placed
fixtures receive black, while near-edge fixtures fade smoothly. Included native
fixtures stay in the owned stream with black output so a previous scene cannot
shine through. Visibility attenuates raw RGB16 before existing calibration;
composition, power, native membership and scene playback stay independent of it.
The glow layer uses screen blending so dim emitted colors cannot darken the grid.

Real streaming checks confirmed three black Play outputs and the expected graded
Govee output, with owner power and layout preserved. See
`evidence/sync-soft-visibility.json` for exact tests and acceptance limits.

## Editable Sync visibility and floor constraints

`0.1.0.dev8` shares a validated six-point visibility polygon between Light Studio
and the adapter. Optional v3 `syncZone` stores horizontal positions relative to
half the table width and vertical offsets from its top. Custom points therefore
follow table resizing. Missing/null selects the automatic 15 cm band and lateral
spread. A save from an older client that omits the field preserves a custom zone;
explicit null resets it.

Vertical polygon sections supply the lower/upper smoothstep bounds. Govee retains
analytic segment integration and weighted sample positions; Play and the nine
physical monitor-strip anchors use point visibility. Each samples the original
capture palette before attenuation and calibration. Scene playback remains
independent. Source capture channels, identities, power and native membership
are unchanged. The layout canonicalizer also keeps physical bodies above the
floor; the table has dimensions only and cannot move off the floor.
