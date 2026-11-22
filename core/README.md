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

Referenced files retain the existing live adapter formats. The two local token files
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
and `timezone`. `entertainment` requires `hue_strip` and `hue_play`: Desk
Entertainment uses all six virtual lights. With a `gateway` section the adapter `bind` must not be a
loopback address: the container reaches the host through its network. Rendering and readiness never load, start or stop services;
loading the agents and starting the compose project is a separate, owner
authorized activation. Design: `docs/design/2026-11-17-core-managed-service.md`.

## Compatibility and operation

The `bridge/live_adapter.py` command retains its existing flags.
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
`/api/live`. Fresh Off/stale/unknown data masks glow. Retained idle receiver
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

### Native Hue power from the Hue app

The virtual bridge's Hue Monitor and Hue Play lights (slots 3–6) used to
forward only color and brightness. On/Off from the Hue app was accepted but
never reached the lights.

An explicit On/Off for these lights now goes to the adapter's authenticated
`POST /native-power` with `{"roles": {"strip" | "hue_left" | "hue_center" |
"hue_right": bool}}`. This applies to a single light and to a room.
- The adapter switches only the pinned profile resource with a separate
  `{"on": {"on": …}}` write. The color path still refuses power changes.
- The write counts as confirmed only after a read-back shows the new state.
  Turning a light on reapplies its composition color.
- An unconfirmed write, or a write during the owner's live-output hold,
  returns 503. The virtual light then shows the real state again: the
  pre-request value is restored first, and fresh native readings correct any
  light whose write took effect anyway, such as part of a room request.

The virtual lights also mirror each proxy's native power when the reading is
fresh (no older than 5 seconds), so an Off from Apple Home appears in the Hue
app. Mirroring pauses for a light while its own write is in flight. Govee
power keeps its owner or HomeKit authority path. Scene recall still never
changes power.

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
4. Renders supervisor files whose paths go through `BASE/current`, and records
   the digest of `core.json` they were rendered from (`services/config.sha256`).

When `core.json` changes, run `stage` again with the same archive: it renders
the supervisor files of a staged release again. It refuses for the active
release, because its running services were started from those files. A release
rendered before digests existed is not treated as stale; it records a digest
when it is staged again while inactive. `activate` and `rollback`
also render a release again when its files come from another configuration;
they refuse when the release is of another version than the running archive.
`activate` of the active version then needs stopped components and prints the
start commands, like any other transition.

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

### NAS container profile

An optional `container` section in `core.json` selects the NAS profile
(`docs/design/2026-11-20-nas-container-profile.md`, issue #9):

```json
"container": {
  "lan": {"parent": "eth0", "subnet": "192.168.10.0/24",
          "router": "192.168.10.1", "adapter_ip": "192.168.10.51"},
  "core_subnet": "172.30.250.0/29",
  "studio_hosts": ["192.168.10.5", "diskstation.local"]
}
```

`stage` then renders one `services/compose.json` and no launchd agents. Docker
runs the gateway, the adapter and Light Studio from the pinned diyHue image
with `restart: unless-stopped`:
- The gateway (`gateway.lan_ip`) and the adapter (`lan.adapter_ip`) get their
  own LAN addresses on a macvlan network. Hue clients need TCP 80/443 and Govee
  replies use UDP 4002; the NAS and its Govee plugin hold those ports.
  Choose both addresses outside the router's DHCP pool. `gateway.mac` stays the
  bridge identity; Docker derives the interface MAC from the address.
- All three share the core network (`core_subnet`, gateway `.2`, adapter `.3`,
  Light Studio `.4`). The NAS host, Homebridge and Light Studio use these
  addresses; a host cannot reach its own macvlan containers. The adapter
  listens on its core address only.
- Light Studio is published on `setup.port` of the NAS and accepts the Host
  names in `studio_hosts` besides loopback. It has no password yet (#53). Its
  Sync guard restarts only Sync, never the bridge container.
- Configured files must lie in the configuration directory or `state_dir`.
  These and the release are mounted at their host paths.
- An owner `live-output-hold` keeps the adapter waiting instead of exiting.

The installer commands need Python 3.10 or later. Without it on the NAS, run
them in the pinned image with the installation mounted at its own path, for
example `docker run --rm --network host -v BASE:BASE --entrypoint python3 IMAGE
-I -B ARCHIVE status --base BASE`. Host networking lets the stop check reach
the core addresses. Start with
`docker compose -f BASE/current/services/compose.json up -d --force-recreate`.

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

On the Mac installation, stop Sync Box before recreating the bridge container, wait for
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

## Two configurations (layout version 5, current version 6)

Light Studio edits two independent configurations. With Sync uses `scale`,
`syncZone` and `syncFalloff` (default on). Without Sync (scenes from the Hue app)
uses row `sceneScale`, `sceneZone` (default automatic) and `sceneFalloff`
(default off). `sceneScale` is the row's own value and never follows `scale`: a
row without it gets a copy of `scale` when it is validated, so every read and
write carries it (issue #44). A fade that is off gives every gain 1 and places
Govee segment samples at their centres. The scene controller and the Hue scene
proxies multiply the scene correction by the scene fade: per Govee segment (bottom
to top), the strip's mean anchor gain and one gain per Play. Hue dimming never goes
below 0.1 %. `LayoutStore.present()` is scene membership (every lamp that is not
removed); `enabled()` stays the Sync composition (own-light and off lamps
excluded). The virtual bridge forwards scenes to every present lamp. Version 3 and 4
files load as version 5.

## Lamp controls

### Off during Sync

A role lamp with `syncRole: "off"` (role rows only) is outside the Sync
composition and is switched off for each Sync session. A session starts when the
video output becomes active and ends when it is released (explicit stop or grace
elapsed). At the start the adapter records each present "off" lamp's power and
switches it off; at the end it switches the recorded lamps on again.

- A lamp that is already off (or whose power is unknown) is not switched and stays
  as it is.
- An owner power change during the session wins: HomeKit (`/homekit`), Hue app
  power for Govee (`/power-request`) or native Hue (`/native-power`), or a native
  Hue light switched on directly at the owner's bridge. That lamp is not restored.
  A HomeKit or Hue app On during the session switches the lamp on at once.
- Govee: a Sync hold in the power authority. The hold is not owner intent, is
  never adopted from native readings, and closes the color writer while it is
  active; `GET /homekit` keeps showing the owner's intent (`sync_hold` appears in
  the `/status` power snapshot). Power uses the Govee `turn` command. Without a
  power authority (no owner credential) the adapter has no power path, so Govee
  "off" lamps are left untouched and keep their own light, like "own".
- Native Hue: the confirmed `{"on": {"on": …}}` write of `/native-power`. The
  role is outside the composition, so the Sync safety fence does not require it
  to be on.
- Crash safety: the roles switched off are written to
  `<state-dir>/sync-off-session.json` (`{"version": 1, "roles": [...]}`, atomic,
  0600) before any command. On start, while no Sync runs, the adapter restores
  them (a Govee lamp after its first fresh native reading, up to 15 s) and deletes
  the file. A clean shutdown restores immediately. A failed restore stays in the
  file and is retried every second.
- Changing a lamp's `syncRole` while a session runs applies at once: a lamp set
  to `"off"` is switched off, and a lamp that leaves `"off"` gets back the power
  this session took from it. A lamp is switched off at most once while it stays
  `"off"`, so an owner who switches it on again keeps it on.

### Blink

`POST /identify` with `{"role": "left" | "right" | "strip" | "hue_left" |
"hue_center" | "hue_right"}` and the `X-Adapter-Token` header blinks one lamp.
It returns 200 `{"accepted": true}` after the blink, 409 `{"accepted": false,
"reason"}` when refused, 503 when a command was not delivered and 400 on bad
input. While Sync drives a lamp it refuses when that lamp's stream has stalled
(no DreamView frame to a Govee in the last two seconds, no native frame in the
last second) with `sync active`. It also refuses while the lamp is
off or not freshly observed (`lamp off`), while its color writer is closed
(`writer closed`), while another blink of the same lamp runs (`busy`) and for an
unconfigured native role (`unavailable`).

- Govee: two short dips (to 10 %, or a third of a dim lamp's brightness, 300 ms
  each) through the power-gated color sender, then the exact previous brightness.
  While a blink runs, the scene and Sync writers' commands to that lamp are
  rejected and resent after the blink; other lamps keep rendering. While Sync
  drives the lamp it is in DreamView and ignores a brightness change until the
  next frame, so the blink is two white flashes in DreamView frames instead
  (white, dark, white, dark, 300 ms each) and the next Sync frame restores the
  picture.
- Native Hue: the Hue v2 `{"identify": {"action": "identify"}}` write on the
  pinned light, through its own client method (the color write still refuses
  power and identify). While Sync streams to the light it shows only the
  Entertainment stream, so the relay shows the same two white flashes (white,
  dark, white, dark, 300 ms each) on that light's stream channels instead; the
  next frame shows the Sync colors again, and Stop ends any flash.

Setup Studio forwards `POST /api/identify {"lamp"}` here.

## Lamp names

A role row of the layout may carry `name` (see `studio/README.md`;
1-35 characters, 1-32 for `hue-monitor` and `play-*`, stripped, no control
characters). `LayoutStore.names()` returns `{role: name}` for the rows that have
one. A missing name means "no owner name": nothing is renamed anywhere, and
removing a name later does not undo earlier renames. A name is applied where
the layout name changes, and once at start:

- Virtual Hue bridge (`bootstrap.py`, `hue_names.OwnerNames`): the virtual light
  of the role (slots 1-6, `hue_names.SLOT_ROLES`) takes the name, which diyHue
  serves as both v1 `name` and v2 `metadata.name`; the bridge configuration is
  saved and an `update` event with the light's `id`, `id_v1`, `type` and
  `metadata` goes to the Hue app through the normal diyHue event queue (and the
  event journal). Resource IDs never change. The layout is checked every
  half second. A name that the owner later sets in the Hue app stays until the
  Light Studio name changes. The Hue app may show its cached name until it
  refreshes; the Sync box reads the name when it next lists the lights.
- HomeKit: `GET /homekit` adds `name` to each lamp (`left`, `right`) that has an
  owner name, and omits it otherwise. The signed body and `X-LCC-Response`
  are computed as before; the API version stays 1 (the field is optional). The
  Homebridge plugin renames the accessory display name, the AccessoryInformation
  `Name` and, only where present, the Lightbulb `Name` and `ConfiguredName`.
  UUID, AID and IID never change. See `homebridge/README.md`.
- Physical Hue lights (`hue_strip_proxy.py`): `PinnedHueClient.rename(name)`
  sends only `{"metadata": {"name": name}}` to the pinned light; the color
  `write()` allowlist is unchanged. `HueStripProxy.sync_owner_name()` runs in the
  proxy loop. It reads the light and writes only when the owner name differs
  from the light's name and from the name the proxy last applied or read, so it
  does not write on every loop, without an owner name, or at start when the
  light already has the name. A failed write is retried every 30 seconds. A
  name longer than 32 characters is refused by the client (the layout already
  refuses it for Hue lamps), never cut.
