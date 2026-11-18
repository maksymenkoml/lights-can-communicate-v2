# Own Homebridge plugin for the desk Govee lamps

Issue: #6. The project is named `lights-can-communicate-v2`; the plugin
follows that name. Owner decisions (2026-11-17): replace the version-specific overlay
with our own Homebridge plugin; the third Govee accessory (Table Lamp) may be
re-added in Apple Home once.

## Why

The overlay patches `lib/device/light.js` of an exact
`@homebridge-plugins/homebridge-govee` release and wraps its LAN, AWS, BLE
transports and command queue. Every plugin release can break it (11.40.0 did).
Two writers also remain: the Govee plugin sends power and Home colors, the core
sends scene and Sync colors, and an ordering protocol keeps them apart.

## Live inventory (NAS, read-only, 2026-11-17)

The Govee platform runs as one child bridge with three accessories:

| Accessory | Model | Services |
|---|---|---|
| Mykola office L | H6076 | AccessoryInformation; Lightbulb (no subtype) |
| Mykola office R | H6076 | same |
| Table Lamp | H6022 | same |

Lightbulb characteristics: On, Brightness, Hue, Saturation, ColorTemperature
and the three Adaptive Lighting characteristics. The platform uses a Govee
account (cloud) and LAN. Accessory UUID = `hap.uuid.generate(deviceId)`.

## Goals

- Apple Home keeps the existing L/R accessories: same bridge identity, AIDs and
  IIDs; rooms, scenes and automations continue without re-pairing.
- The core is the only writer to the L/R lamps: power, brightness and color.
  No ordering protocol between two writers is needed any more.
- No dependency on Govee plugin internals or versions.
- Hue App power requests (#22) and virtual Hue power feedback keep working.
- A rehearsed migration and rollback; the live migration needs separate owner
  authorization.

## Non-goals

- Table Lamp control by the core (it stays in the Govee plugin).
- Light Studio inside Homebridge (#5) and enrollment of other devices (#7).
- The NAS move of the core (#9).

## Design

### Plugin `homebridge-lights-can-communicate-v2` (`homebridge/` in this repository)

- Dynamic platform `LightsCanCommunicateV2`, ESM, no runtime dependencies besides the
  Homebridge API. Configuration: core URL, credential file in Homebridge
  storage, and the two lamps (`role`, Govee `deviceId`, `name`).
- For each lamp it registers `uuid.generate(deviceId)` with the cached name,
  AccessoryInformation (Govee manufacturer/model/serial values) and a Lightbulb
  with On, Brightness, Hue, Saturation, ColorTemperature and an
  `AdaptiveLightingController` in automatic mode. HAP-NodeJS assigns AIDs and
  IIDs from the bridge `IdentifierCache` by accessory UUID, service type and
  subtype, and characteristic type; identical keys give identical numbers.
- Setters send the intent to the core; getters and updates come from the core
  state (polled about once per second), never from optimistic local values.

### Core

- New route `POST /homekit` (power-owner credential) with role and any of
  `on`, `brightness`, `hue`/`saturation`, `color_temperature`; `GET /homekit`
  returns the persisted intent and verified native readings per role.
- The core executes power itself through the existing Govee LAN path:
  Off = close color permission, drain in-flight sends, send Off, confirm native
  Off; On = send On, confirm native On, then open color permission. Brightness
  is sent natively. Home color/CT becomes the lamp's static color while no
  scene or Sync owns it; during a scene or Sync it is stored and the active
  output keeps the lamp.
- Power intent is persisted in `state_dir`, so a restart cannot revive a lamp
  that Home turned Off.
- Hue App power requests execute in the core directly; the plugin sees the
  result in the next state poll and updates Home.
- The legacy owner heartbeat (`/power-owner`) stays until the cutover. A
  configuration flag selects which authority owns L/R power.

### Migration (rehearsed offline, live only with owner authorization)

1. Back up `config.json`, `accessories/` and `persist/`.
2. Stop the Govee child bridge. Move the Govee platform to a new child bridge
   identity and add L/R to its ignored devices. Give the `LightsCanCommunicateV2`
   platform the old Govee child bridge `username`, `pin` and `port`.
3. Start both bridges. Home sees L/R unchanged on the old bridge. Table Lamp
   appears on the new bridge and is added in Home once.
4. Switch the core to own L/R power, then remove the overlay by reinstalling
   the original Govee plugin release. The private owner config is removed.
5. Rollback: restore the backed-up `config.json` and restart; the Govee plugin
   regains L/R with the same UUIDs on the old bridge.

The old bridge must never be published without L/R, because HAP-NodeJS drops
unused identifiers when it saves the cache.

## Verification

- Unit tests for the plugin with the real HAP-NodeJS: characteristic set,
  adaptive lighting, setters to core, state updates, errors as HomeKit
  communication failures.
- Identity rehearsal: a copy of the live `IdentifierCache` and `AccessoryInfo`
  (private, never committed) loaded by a local bridge with the plugin; every
  L/R AID/IID must match and no new identifier may appear for them.
- Core tests: ordering (permission closed before Off, native On before color),
  persistence across restart, Hue requests, rejection of unknown roles.
- Live acceptance after authorization: Home Off/On/brightness/color for L/R,
  Home scenes, Hue App On/Off, Sync and scene output after On.

## Builder's calls

- Core as the single writer instead of the plugin sending LAN power itself:
  on the NAS both would need the Govee reply port 4002.
- Home colors apply only while no scene or Sync owns the lamp.
- State polling instead of a push channel, to keep the first version small.
- Note for #9: the Govee plugin also listens on LAN port 4002 for Table Lamp;
  on a shared NAS host its LAN mode must be disabled or Table Lamp moved to the
  core.
