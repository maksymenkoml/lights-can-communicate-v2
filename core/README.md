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
```

The only supported runtime is the NAS container profile: Docker Compose runs the
adapter, Light Studio and the virtual bridge from the pinned diyHue image (see
"Managed service release" and "NAS container profile"). `--check` runs
anywhere with Python and needs no network. Starting the adapter outside its
container is not a supported deployment.

Python 3.10+ is required. Native video also requires OpenSSL; preflight checks its
availability before starting the adapter. No pip/runtime library installation is
required. Keep `-I`: it excludes Python environment and working-directory import
injection. The archive contains the explicit public runtime allowlist and a
SHA256 source manifest, with fixed zip metadata. Existing output paths are never
overwritten. Build a new version alongside the installed artifact for rollback.

Use `config.example.json` as the service configuration structure. All paths,
including `state_dir`, resolve relative to the configuration file. Optional Hue
configuration fields can be omitted for Govee-only operation. Native video
requires Hue configuration, `hue_play` and a native Hue profile in the light
registry (see "Light registry"). Omit all Hue fields for Govee-only operation.
`adapter.targets_file` and `adapter.native_profile_file` are optional: they are
inputs of the one-time registry migration only. The `gateway` and
`container` sections are mandatory: a configuration without them is rejected.
The adapter listens on its core-network address only (`adapter.bind` may only
repeat it).

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
blocks actual startup. The room layouts are read from that same state directory.

## Rooms

Lights are grouped in rooms (#78). `rooms.json` in the state directory lists the
rooms in order, the room of each connected light and the room of the Sync Box
(`sync`, or `null`). Each room's layout is `rooms/<room id>.json` in the version
seven layout shape; its `room` field is the room's name. A connected light that
the index does not list is in the first room.

A light's corrections, name, calibration and scene fade come from its row in its
own room. Sync uses the Sync Box's room only: its layout gives the Sync profile,
and only its lights take part in Sync. Without a Sync room no light takes part.
A room refuses a row of a light that the index puts in another room.

Before the first layout write there is no index: the single `desk-layout.json`
is read as the first room (`room-00000001`), which holds every light and the
Sync Box, so behavior is unchanged. The first write from Light Studio creates
`rooms.json` and the room file and removes `desk-layout.json`. An unreadable
index keeps the last good one; without one no light takes part in Sync. The
state schema `rooms` is 1, so a rollback restores the state snapshot taken
before the upgrade.

Light Studio manages the rooms through the room cards of its page (see
`studio/README.md`) and its API:

- `GET /api/rooms` lists the rooms in order with their names, the Sync Box's room
  (`sync`), the room of every connected light (`lights`), every recorded room of
  a light (`assigned`; any other light is in the first room) and the room whose
  layout has each light's row (`placed`).
- `GET` and `POST /api/layout?room=<id>` read and write a room's layout (the first
  room without `room`); a write still needs `If-Match`.
- `POST /api/rooms/create {"name"}` adds a room at the end with the default desk
  and screen; `rename {"room", "name"}` renames it (names have 1-40 characters);
  `order {"rooms"}` sets the order.
- `POST /api/rooms/delete {"room"}` deletes a room without connected lights and
  without the Sync Box; its layout goes with it. The last room stays.
- `POST /api/rooms/move {"light", "room"}` puts a light in another room; its row
  moves with its corrections. `POST /api/rooms/sync-box {"room"}` registers the
  Sync Box in a room (`null`: in no room). A move of a Hue light or of the Sync Box
  lets the adapter move the Hue lights into or out of the area (`/lights/rooms`,
  which restarts the adapter when the area changes); a refusal undoes the change.
  A Govee light has no area, so its move does not wait for the adapter.
- `POST /api/lights/enroll {"id", "room"}` connects a light in that room.

Creating, deleting and moving rooms, lights and the Sync Box restart the virtual
bridge or change the area, so they are refused with `sync_active` while Sync
output owns the lights. Renaming and reordering are allowed then. Other
refusals: `invalid_name`, `room_limit` (ten rooms), `unknown_room`,
`unknown_light`, `last_room`, `has_sync_box`, `has_lights` and `layout_full`
(twelve rows).

The core's Entertainment area holds only Hue lights of the Sync Box's room. A
Hue light of another room is connected for scenes only: it is in no area, has no
channels, and the adapter drives it through the CLIP API, during Sync as well.
After a change of the rooms, `POST /lights/rooms` on the adapter (authenticated,
body `{}`) moves the Hue lights into or out of the area as their rooms and the
Sync Box's room require, saves the registry and restarts the adapter. It answers
`{"accepted": true, "restarting": ..., "changes": {light id: null or reason}}`;
`null` means the light joined the area. It is refused with `sync_active` while
Sync output is active or the area streams, and an area change that fails is
undone.

Each room is a Room group on the virtual bridge, named as the room, with the
virtual lights of its connected lights. The first room keeps group "1"; a new
room's group takes the lowest free group id from 2 and the `id_v2`
`uuid5(URL, "smart-lighting-probe/<room id>")`, with the Hue class Other.
`room-groups.json` in the bridge state records which group belongs to which
room; the gateway changes or deletes only those groups. A light is in its room's
group only. A Hue app scene of a room never drives a light of another room: a
light that leaves a group is removed from the group's scenes. Deleting a room
deletes its group and its scenes. A removed light's virtual light stays where it
is. A renamed room renames its group without a restart. The Sync Box's stream to
group "90" does not change. During Sync only the lights of the Sync Box's room
follow the stream: the Govee lights of the other rooms keep their scenes and
HomeKit power.

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
into a new directory only. It writes `compose.json`, one Compose project that
holds the gateway, the adapter and Light Studio (see "NAS container profile").

Commands of the archive: `adapter` (default), `setup` (Setup Studio on
`setup.bind:setup.port`, default `0.0.0.0:19881` inside its container),
`ready` (exit 0 once the adapter `/status` and the bridge `/api/config` answer
on the core network; prints a credential-free summary) and `services`. An
active `state_dir/live-output-hold` keeps `adapter` waiting until the hold is
removed, because Docker restarts a finished container whatever its exit code.

Required configuration sections:

```json
"gateway": {"lan_ip": "192.168.1.20", "mac": "02:00:00:00:00:01",
            "live_output": true, "hue_strip": true, "hue_play": true,
            "entertainment": true, "gradient": true},
"container": {"lan": {"parent": "eth0", "subnet": "192.168.1.0/24",
                      "router": "192.168.1.1", "adapter_ip": "192.168.1.21"},
              "studio_hosts": ["diskstation.local"]}
```

and the optional `"setup": {"port": 19881}` (`setup.bind` may only be
`0.0.0.0`).

`adapter.power_authority` selects who owns Govee power: `owner` (default, the
remote heartbeat of the Homebridge overlay) or `homekit` (this adapter executes
power for the `homebridge-lights-can-communicate-v2` plugin through `/homekit`;
`/power-owner` is then closed). See `homebridge/README.md`.

`gateway` also accepts `container_name`, `image` (a pinned `diyhue/core`
digest), `adapter_url` (default `http://<adapter core address>:<adapter port>`)
and `timezone`. `entertainment` requires `hue_strip` and `hue_play`: Desk
Entertainment uses all six virtual lights. Rendering and readiness never load,
start or stop services; starting the compose project is a separate, owner
authorized activation. Design: `docs/design/2026-11-17-core-managed-service.md`.

## Compatibility and operation

The `bridge/live_adapter.py` command takes `--registry PATH` (the light
registry) instead of `--targets` and `--native-profile`; a registry with Hue
lights requires `--hue-config`, and `--hue-config` requires them. Its state
directory defaults to the registry's parent, and its bind
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
frames. It covers exactly the registry lights that `/status` lists, in registry
order (an older adapter without the list shows the six migrated lamps first); with
a Sync profile a later light is included while its layout row is composed. `/status` also lists the
enrolled lights for Light Studio in `lights` (id, provider, model, zone count and
whether it is migrated), without device identities or addresses. These are command colors, not optical measurements or segment readback.
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

## Light registry

The core owns `state_dir/lights.json`, the registry of the enrolled lights: for
each light its ID, provider, virtual bridge slot and identity seed, its Govee
target fields, or its Hue light UUID with the native Entertainment channels, plus
the Hue area UUID. The adapter reads only this file (`--registry`). The registry
is strict: unknown fields, duplicate keys, invalid UUIDs and an incomplete set
of the four migrated native lights (`hue-monitor`, `play-left`, `play-center`,
`play-right`, all or none) are rejected. Govee lights are any number of entries
whose ID is `govee-left`, `govee-right` (the migrated lamps) or `device-`
followed by 32 lower-case hexadecimal digits (the discovery ID). A Hue light
enrolled later also has a discovery ID and keeps its native model; a registry
needs at least one light. The file holds no credentials and is written
atomically with owner-only permissions. The adapter also writes it when it
follows a lamp to a new Govee address. Only an entry in the registry makes a
Govee light an output; discovery never does.

### Enrolling and removing lights

The owner enrolls a discovered Govee or Hue light with `POST /api/lights/enroll` and
removes one with `POST /api/lights/remove` in Light Studio (body `{"id":
"device-<32 hex>"}`, or a migrated id such as `"play-center"` for a removal; local
origin only). Light Studio forwards the request to the
adapter (`/lights/enroll`, `/lights/remove`, adapter token), the only writer of
the registry. An enrollment needs the light to pass the binding rules at that
moment: discovered, online, at one address that no other device uses, and not
one of the enrolled migrated lamps. Any model qualifies; a model outside the capability
table is driven as a basic light. The new entry takes the Govee device ID, the
current address and the model; its virtual slot is the lowest free `id_v1`, with
the light ID as the identity seed. A removal keeps the light's virtual identity
and its physical identity (the Govee device key or the Hue light UUID) in
`retired` (at most 64 records, oldest dropped first), so a rejoin of the same
device gets the same slot and seed.

Any enrolled light can be removed, the six migrated lamps too; the registry may
hold any set of lights, or none, and keeps the paired Hue area without Hue
lights. Without Hue lights the adapter still starts with the paired bridge, so
a Hue light can be enrolled again, and Sync drives the Govee lights only. The
row of a disconnected migrated Hue light is not a native Sync role, so Sync does
not wait for a light that has no output. A removed migrated Hue light leaves the native area like any Hue light,
and the later channels move down. Its virtual light stays in the bridge and in
the Sync Box's Entertainment area, so the stream topology does not change, but
nothing drives it and HomeKit shows it as not responding. Discovery names a
removed migrated lamp by its migrated id (`layout_id`) and keeps it enrollable;
enrolling it under its discovered id gives it its migrated id, slot and seed
back, so its HomeKit accessory responds again. The enrollment answer names the
registry id (`"id"`).

A Hue light of the Sync Box's room is enrolled into the core's native
Entertainment area (a light of another room is connected for scenes only, see
"Rooms"): the area in
the light registry (an existing pairing's area is adopted as the core's), or one
that the core creates for the first Hue light. The core creates the area named
"Light Studio" (configuration type `screen`) with that light as its only member;
when the light cannot join it on its own (shared channels, no channel), the core
deletes the area again, and it deletes it as well when the enrollment fails
afterwards. When the registry names an area that the bridge no longer has (after
a pairing with another bridge) and no Hue light depends on it, the first Hue
light replaces it with a new area. The core never changes an area that it does
not own. With Sync off, the adapter adds the light to the area through the
bridge with the paired credentials. The
bridge merges lights at one position into one channel, so the light takes the
free point farthest from the area's channels; otherwise positions in the area do
not matter, because the Light Studio layout gives the light its Sync zone. The
bridge places every gradient light on the same fixed template, whatever its
position and the area type, so a second gradient light shares the first one's
channels (verified on a Hue Bridge on 2026-11-23). A light whose channels are
shared cannot be driven on its own: the area is restored, and the light is
connected for scenes only. The light's entertainment service is the
one whose renderer is the light, and its channels are the area's channels of
that service. A light that is already a member is kept as it is. The registry
appends the light's channels after the channels of the lights enrolled before
it, so the migrated channels and their packet bytes do not change; removing a
Hue light moves the channels of the later ones down. A removal takes the light
out of the area before the registry changes and puts it back when the registry
cannot be saved; an enrollment that is refused after the area changed takes the
light out again, and a failed or uncertain area write restores the earlier
locations. Enrollments and removals run one at a time.

Every client that sends the application key authenticates the bridge first: the
area client, the light proxies, the native relay's area requests and Hue
discovery. They share one verified connection (`bridge/hue_link.py`). It checks
the certificate chain against the Signify root CA for Hue bridges (`root-bridge`),
and the certificate's common name is the bridge id. The first verified
connection pins that id in `state/hue-bridge-id` (pairing in Light Studio writes
it too); every later connection must name the same bridge, otherwise nothing is
sent. A bridge whose certificate is not issued by that CA gets no key.

When the bridge stops answering at its address, or another device answers there,
the connection looks for the pinned bridge id among the Hue bridges that answer
mDNS on the LAN (from the adapter's LAN address), at most every 30 seconds, and
follows it to its new address. The Sync stream (DTLS, authenticated by the
client key) connects to that current address. The address is followed in
memory: after a restart the configured address is tried first. Without a pin the
connection never moves. After a bridge replacement, pair the new bridge in Light
Studio (or delete the pin file). The relay refuses an area with a member that is
not enrolled, so do not add lights to this area in the Hue app.

A change is refused with 409 and a reason while Sync output is active
(`sync_active`), when the light is already enrolled (`enrolled`) or not enrolled
(`not_enrolled`), when discovery does not offer it now or the area cannot be read
or changed (`unavailable`), and after a change until the restart (`restarting`).
A Hue light that the area cannot take is not refused: when the area would exceed
ten lights or twenty channels, or the bridge gives the light shared channels, it
is connected for scenes only. The registry keeps such a light with no channels
and the reason (`"outside"`: `room`, `channels_shared` or `area_full`), and the
adapter lists the reason with the light. A
native cleanup that is still pending does not block a change: it can wait on an
area that only the change repairs, and the area itself is not changed while it
streams. A changed registry is saved atomically,
then the adapter stops and Docker restarts it with the new light set. The
gateway also restarts when the set of virtual lights changes (see "Virtual
lights"). Place the light in the Light Studio layout to give it a
Sync zone, and add `{"deviceId": ..., "name": ...}` to the Homebridge plugin's
`lamps` for a HomeKit accessory.

### Virtual lights

The gateway reads the registry at start (the release mounts the core archive
read-only next to its modules; without a registry it keeps the two migrated
lamps) and creates one virtual Hue light for each enrolled light. The
migrated lamps keep slots 1 and 2 and their UUIDs. A light enrolled later gets
the virtual slot from its registry entry and a UUID seeded from its registry ID
(`uuid5(URL, "smart-lighting-probe/<seed>")`). A segmented light is a gradient
light in the gradient mode; any other light is a color bulb (`LCT015`). A Hue
light enrolled later mirrors its native light: a gradient light (`LCX006`) with
several channels, else a color bulb; its scenes and power go to its native
light through the adapter. New
lights join the Room group of their room (see "Rooms"), so Hue app scenes, group
power and the Hue app reach them, but not the Entertainment area: the Sync Box pairing and the sampler stay
as they are, and Sync drives the new light by projection. Scene output, power
requests and feedback, scene Play replay and owner names use the registry slot
map instead of fixed slots. A removed light keeps its virtual light, unreachable
and not driven, so a rejoin gets the same light and its scenes. The gateway
checks the registry and the room index every two seconds and restarts (Docker
starts it again) when the virtual lights, the rooms or the rooms of the lights
change; an address update or a renamed room does not restart it.

### Govee lights and model tiers

The adapter addresses every Govee light by its registry ID: `/command`,
`/identify`, the power gates, HomeKit state, Sync-off records and `/status`
targets (`light`) all use it. A `/command` for an ID that is not in the registry
is refused with 400 and sends nothing. The fields of a Govee entry are the old
target fields (`device`, `ip`, `brightness_scale`, `sku` or `model`, rate
limits, `segment_count`) and the optional `tier`.

`bridge/govee_models.py` is the capability table with the tested values of each
model: tier, segment count, DreamView frame rate, gradient packet limit,
DreamView idle and loss timing and the zone geometry. Today it holds the
H6076. Tiers:

- **segmented**: a model in the table. With a Sync profile (or the gradient
  mode) the adapter enables its segments and the bounded gradient packet rate,
  and Sync streams DreamView frames with the table's timing.
- **basic**: any other model, or any light with `"tier": "basic"` in its
  registry entry (the only value an owner can force; it wins over the table and
  over a stored `segment_count`). A basic light has one color for the whole lamp
  through `colorwc` plus brightness and power. It never gets segment, gradient
  or DreamView packets. Sync gives it the projected color of the center of its
  visible zones. A model that is not in the table keeps a `segment_count` that
  its entry declares itself, but nothing enables segments for it automatically.

The basic tier has no default command rate: it uses `max_commands_per_second`
of its entry when present, else the adapter's 20 Hz render tick with change
detection. The migrated lamps behave as before.

`/power-owner` (the obsolete Homebridge overlay) keeps its side names: it names
the two migrated lamps `left` and `right`, and the adapter translates at that
HTTP boundary (`bridge/legacy_wire.py`) and shows no other light there.
`GET`/`POST /homekit` (the Homebridge plugin, API version 2) names every Govee
lamp by its Govee device ID: `GET` answers `{"version": 2, "lights": {deviceId:
{...}}}` and `POST` takes `{"device": deviceId, ...}`. The adapter resolves the
device ID through the registry and refuses an unknown one with 400. Everything
inside, including `/power-request` (`states` by light ID, unknown IDs refused
with 400), the `/status` power snapshot (`power_owner.lights`) and
`state_dir/homekit-power.json`, uses light IDs. The HomeKit file is
`{"version": 2, "lights": {id: {...}}}`; a version-one file (`roles` by side)
is converted once on the first start and rewritten, and its old keys are not
read afterwards.

The first start without `lights.json` migrates once, in memory, from
`adapter.targets_file` and the native profile (`adapter.native_profile_file`),
checks the whole configuration and only then writes the registry. The six lights
keep their IDs, virtual slots 1-6 and identity seeds, and the Govee target
fields (calibration scale, model and rate limits) carry over verbatim. The
adapter then gets the same targets and the same native profile as before.
`--check` migrates in memory and never writes. After `lights.json` exists the old
files are never read again, and a corrupt registry stops the start instead of
falling back to them. The migration never edits or deletes the old files.
Rollback restores the pre-migration state through the activation snapshot (see
"Installation, upgrades and rollback"), which removes `lights.json` together with
the newer release.

## Native Hue selection profile

The initial `0.1.0.dev4` extraction introduced optional
`adapter.native_profile_file`. The profile is now a migration input of the light
registry (see "Light registry"). The registry holds a list of native Hue lights,
each keyed by its registry light ID (`hue-monitor`, `play-left`, `play-center`,
`play-right` for the migrated lights), with its native light UUID and its
ordered channels (canonical channel ID, entertainment service UUID, segment
index), and the native Entertainment area. The loader builds the profile from
that list: canonical channel IDs number the area's channels from zero, all
channels of a light share one service and their indexes follow the segment
order, and an area holds at most ten lights and twenty channels. The profile file resolves
relative to the service configuration and is read only while `lights.json` does
not exist. The loader
rejects duplicate/unknown fields, invalid UUIDs, overlapping resources and changed
topology. Credentials stay in their existing separate files.

An explicit profile controls each client/proxy/relay instance; it overrides old
credential light/area hints. The runtime contains no physical identity defaults.
Resource membership, channel order/index and resource types must match live metadata before stream
start. A registry with Hue lights and no Hue configuration fails before
initialization. A Hue configuration with no Hue light in the registry is a paired
bridge without Hue output: the adapter starts, discovery reads the bridge's
lights, and Sync drives the Govee lights only.
The adapter creates one proxy per native light and addresses every native light
by its registry light ID: `/command` (`light`), `/native-power`, `/identify`,
calibration, Sync Off and `/status` (`"hue": {<light ID>: snapshot}`). The
relay derives each light's channels from the profile and keeps the packet bytes
of the migrated area. A Hue light enrolled later (see "Enrolling and removing
lights") has no capture sources: Sync projects one zone per channel from its
layout row, spread 10 cm apart along its orientation around its position, and the
relay sends each channel the zone at its place.

The owner's same-selection profile replaced dev3 locally after read-only metadata
parity. Live scene, PS5 Sync and Stop-to-scene checks passed on dev4; all six roles,
calibration, HomeKit identities, pairing and geometry were retained. The NAS was
not changed. The previous archive/configuration remain available for rollback.
See `evidence/native-hue-profile.json` for tests, live frame samples and limits.

### Pairing the Hue bridge from Light Studio

The Lamps panel has a Hue bridge section. Connect looks for bridges on the LAN
(mDNS `_hue._tcp`) or takes a typed private IPv4 address, and Light Studio then
asks the owner to press the bridge's link button within 30 seconds. Every
request verifies the bridge certificate against the Signify root CA, and the
bridge must name the same bridge ID in its answer as in its certificate.

Pairing writes `hue-bridge.json` (mode 0600) in the state directory: the
address, the bridge ID, its name, the application key, the streaming client key
and the application ID. It also writes the `hue-bridge-id` pin. No Light Studio
answer contains a key or the application ID. A bridge with another ID than the
pinned one is refused while the light registry holds a Hue light.

The service uses `hue-bridge.json` for scenes and the Sync stream only when its
configuration names neither `hue_config_file` nor `hue_stream_config_file`;
explicit files always win. With explicit files, the section shows the bridge as
set in the service configuration and offers no pairing; Light Studio does not
read those files. The adapter restarts when the file changes (not
while Sync output owns the lamps), so it starts with the new bridge. The first Hue light
connected on a newly paired bridge creates the core's Entertainment area (see
"Enrolling and removing lights").

### Native Hue power from the Hue app

The virtual bridge's Hue Monitor and Hue Play lights (slots 3–6) used to
forward only color and brightness. On/Off from the Hue app was accepted but
never reached the lights.

An explicit On/Off for these lights now goes to the adapter's authenticated
`POST /native-power` with `{"lights": {<native Hue light ID>: bool}}`. This applies to a single light and to a room.
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
The first start migrates it into the light registry.

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
4. Renders the Compose project whose paths go through `BASE/current`, and records
   the digest of `core.json` they were rendered from (`services/config.sha256`).

When `core.json` changes, run `stage` again with the same archive: it renders
the Compose project of a staged release again. It refuses for the active
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

The required `container` section in `core.json` describes the NAS profile, the
only supported runtime (`docs/design/2026-11-20-nas-container-profile.md`,
issue #9):

```json
"container": {
  "lan": {"parent": "eth0", "subnet": "192.168.10.0/24",
          "router": "192.168.10.1", "adapter_ip": "192.168.10.51"},
  "core_subnet": "172.30.250.0/29",
  "studio_hosts": ["192.168.10.5", "diskstation.local"]
}
```

`stage` renders one `services/compose.json`. Docker
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
  Sync guard restarts Sync on the Sync Box after a bridge restart or a stall,
  never the bridge container.
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

Before recreating the containers, stop Sync on the Sync Box. After `ready`
reports the gateway, start Sync and confirm that frames arrive. The
control-datagram counter and the actual DTLS frame counter are distinct checks.
See `evidence/sync-transport-recovery.json` for a delivery failure observed on
the former Mac host, sequential live observation windows, tests and measurement
limits.

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

## Two configurations (layout version 5, current version 8)

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

## Screen object and per-light projection (layout version 7)

The layout holds its own `screen` object: `positionCm` (`x`, `height`, on the five
centimeter grid) and `inches` (19-55, 16:9). It replaces `monitorInches`, and the
position of the `hue-monitor` row no longer doubles as the screen position. Version
7 requires `screen` and rejects `monitorInches`. Files of version 3-6 load as
version 7: the screen takes the size from `monitorInches` and the position from the
`hue-monitor` row after its minimum height was applied. The screen is
authoritative: the validated `hue-monitor` row always follows its position, and its
height is raised to the same minimum as that row. Light Studio still drags the
monitor lamp; its position is written as the screen.

`bridge/light_geometry.py` turns one layout row and the screen into zones. A zone is
a point in layout centimeters: a segmented floor lamp (a model in `GEOMETRIES`,
today H6076) has seven vertical segments, the `hue-monitor` strip has the nine
anchors along the screen edge, and every other light has one zone at its position.
A row takes its geometry from its role, else from its `model`.

`sync_projection.sync_profile` computes the zone samples on the capture perimeter
and the visibility gains for each light through that one geometry. The profile key
`lights` maps the ID of each light in the Sync composition to its `samples` (none
where a zone is hidden), `gains` and `fade_cm`; `project_light_frame` turns a frame
into the attenuated colors of its zones. The earlier keys (`govee`, `plays`,
`play_gains`, `strip_gains`, `enabled_roles`, `native_roles`) are derived from the
same entries, for every role row the layout holds, so the outputs of the six
current lights are byte-identical. `scene_fade` uses the same geometry. Every row
is optional: a migrated light without a row has no entry in either, and an empty
layout gives an empty composition.

## Room furniture (layout version 8)

Not every room has a desk and a monitor (design:
`docs/design/2026-11-25-room-furniture.md`). Version 8 replaces `desk` with
`furniture`, a list of up to 12 pieces `{"id": "piece-<8 hex>", "kind", "x",
"width", "height"}` in centimeters on the 5 cm grid (x -200..400). A `desk` is
80-240 wide and 50-120 high, a `cabinet` 20-300 wide and 20-150 high. The
`screen` is optional: one display (a monitor or a TV alike, 19-100") that adds `on`,
the id of the piece it stands on or null. Files of version 3-7 load as version 8: the
desk becomes the piece `piece-00000001` at the room axis (x 100) and the screen a
monitor on it, so saved zones resolve to the same world points.

Sync zones are relative to the screen's support (`desk_layout.screen_support`).
Without a support a saved zone is drawn against the floor at the room axis (half
width 100, top 0) and the automatic zone is absent: every gain is 1 and a light
whose geometry needs the screen is projected as a point. The Sync Box needs a
screen: `Rooms.place_sync_box` and a layout write to the Sync room without one are
refused with `no_screen` (HTTP 409 in Light Studio). Light Studio still draws one
desk under a display; it keeps the desk's id and place and passes other pieces
through. `STATE_SCHEMAS["desk_layout"]` is 8, so a rollback restores the
pre-upgrade layout.

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
- Crash safety: the lamps switched off (Govee and native Hue light IDs) are
  written to
  `<state-dir>/sync-off-session.json` (`{"version": 1, "roles": [...]}`, atomic,
  0600) before any command. A file from before the light IDs lists `left`,
  `right` or the native role names (`strip`, `hue_left`, `hue_center`,
  `hue_right`); it is converted once on load and rewritten, and the old names
  are not read afterwards. On start, while no Sync runs, the adapter restores
  them (a Govee lamp after its first fresh native reading, up to 15 s) and deletes
  the file. A clean shutdown restores immediately. A failed restore stays in the
  file and is retried every second.
- Changing a lamp's `syncRole` while a session runs applies at once: a lamp set
  to `"off"` is switched off, and a lamp that leaves `"off"` gets back the power
  this session took from it. A lamp is switched off at most once while it stays
  `"off"`, so an owner who switches it on again keeps it on.

### Blink

`POST /identify` with `{"role": <Govee or native Hue light ID>}` (Light Studio
sends the row ID of one of the six role lamps) and the `X-Adapter-Token` header blinks one lamp.
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
characters). `LayoutStore.names()` returns `{key: name}` for the rows that have
one; the key is the Govee light ID or the role of a Hue lamp. A missing name means "no owner name": nothing is renamed anywhere, and
removing a name later does not undo earlier renames. A name is applied where
the layout name changes, and once at start:

- Virtual Hue bridge (`bootstrap.py`, `hue_names.OwnerNames`): the virtual light
  of the role (slots 1-6, `hue_names.SLOT_ROLES`: `govee-left`, `govee-right`, then the Hue
  roles) takes the name, which diyHue
  serves as both v1 `name` and v2 `metadata.name`; the bridge configuration is
  saved and an `update` event with the light's `id`, `id_v1`, `type` and
  `metadata` goes to the Hue app through the normal diyHue event queue (and the
  event journal). Resource IDs never change. The layout is checked every
  half second. A name that the owner later sets in the Hue app stays until the
  Light Studio name changes. The Hue app may show its cached name until it
  refreshes; the Sync box reads the name when it next lists the lights.
- HomeKit: `GET /homekit` adds `name` to each lamp (keyed by Govee device ID)
  that has an owner name, and omits it otherwise. The signed body and
  `X-LCC-Response` are computed as before; the field is optional. The
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
