# Any selected set of Hue and Govee lights (#7)

Status: design approved for planning. Stage 1, stage 2 (screen object and
generic projection), stage 3a (Govee lights by registry id, model tiers) and
stage 3b (`/homekit` API 2 and plugin 0.2.0 by Govee device ID; only the
obsolete `/power-owner` keeps side names), stage 3c (enrollment and removal
of discovered Govee lights through Light Studio and the adapter) and stage 4
(virtual lights per registry entry; the sampler rule for new installations
moved to #59), stage 5a (list-based native Hue profile, native lights
addressed by registry id everywhere) and stage 5b (enrollment of a Hue light from
the paired area, its projection and its virtual light) implemented. Implementation follows in stages; each stage is a
separate PR with live NAS verification.

## Intent

The core is a product for other owners. Each owner has a different number of
Hue and Govee lights, different Govee models and a different room. Today the
core drives exactly six lights: Govee `left` and `right` (H6076), a Hue monitor
strip and three Hue Play lights. The fixed set is in the virtual bridge, the
Sync path, the Govee adapter, the native Hue profile, power and HomeKit, Light
Studio and the Homebridge plugin.

This work makes an owner-selected set of supported lights the physical scene
and Sync output. It is the foundation for the first-run setup (#59), Hue bridge
pairing (#60) and plugin pairing (#62): those flows fill in what this design
defines.

## Goals

- Any owner-selected set of supported Hue and Govee lights is driven by scenes
  and Sync. The set can be empty on either side (Govee only or Hue only).
- Sync colors for each light come from its placement in Light Studio.
- Govee models have support tiers (below). Any LAN-capable Govee light works at
  the basic tier.
- The current six-light installation keeps its behavior, identities, pairings,
  scenes, calibration and HomeKit accessories.

## Non-goals

- Pairing with the physical Hue bridge and deriving its Entertainment setup
  (#60). This design only generalizes the native profile format.
- The first-run setup flow (#59) and plugin pairing by code (#62).
- Container packaging (#61) and the Light Studio password (#53).
- Replacing the pinned diyHue runtime (#4).

## Anti-goals

- Discovery must never enroll or control a light by itself. Only an explicit
  owner selection enrolls a light (existing #7 rule).
- No permanent second code path for the six legacy roles. After migration the
  six lights are ordinary registry entries.
- No segmented Sync claims for Govee models nobody tested live.
- No change of an existing virtual light ID, Entertainment area ID, bridge ID or
  HomeKit accessory UUID.

## Constraints

- A Hue Entertainment area holds at most ten color lights, and one area per
  bridge is active at a time ([Philips Hue FAQ](https://www.philips-hue.com/en-us/support/topic/entertainment/100085)).
  The Sync Box streams to the virtual bridge's area under the same rules.
- Govee lights use the LAN API (UDP scan 4001, control 4003, status 4002).
  Segmented streaming (razer/DreamView, ptReal) differs per model.
- Identities are seeded with the frozen namespace `smart-lighting-probe`. The
  Hue app and the Sync Box are paired with the existing virtual lights 1-6 and
  the `Desk Sync` area.
- Runtime dependencies stay in the Python standard library.

## Approaches

1. **Single light registry, staged migration (chosen).** One core-owned
   registry describes every enrolled light. Every layer derives from it. The
   work lands in stages; the first stage changes no behavior.
2. **Six roles plus extra lights.** Keep the roles and add slots 7-10 with
   separate handling. Faster at first, but two code paths remain, and an owner
   without the monitor strip or two Govee lights still cannot use the product.
   Rejected because it defeats "any set".
3. **Registry in one rewrite.** Same architecture, one large change. Rejected:
   no intermediate state can be verified live, and this system has shown
   regressions that only live Sync reveals.

The owner chose approach 1 on 2026-11-22.

## Owner decisions

- Other owners use any Hue and Govee lights (2026-11-22).
- Govee support tiers instead of all-models-segmented or tested-only.
- Existing state files migrate once, automatically, on upgrade. Old formats are
  not read after migration.

## Design

### Light registry

A new core-owned state file holds the enrolled lights. Each entry has:

- `id`: an opaque, stable light ID. The six migrated lights keep their layout
  row IDs (`govee-left`, `govee-right`, `hue-monitor`, `play-left`,
  `play-center`, `play-right`). New lights use the discovery ID
  (`device-<hash>`).
- `provider`: `govee` or `hue`.
- `model` and `tier` (Govee) or the native resources (Hue, below).
- the Govee device ID (MAC). The IP address is the last known address; the
  adapter follows discovery when it changes.
- `virtual`: the virtual bridge light (`id_v1` and identity seed). The six
  migrated lights keep slots 1-6 and their current seeds.
- zone count for the light.

Only the core writes the registry. Light Studio requests an enrollment or a
removal; the core validates it against the discovery catalog with the existing
binding rules (discovered, online, supported, unambiguous).

Removing a light keeps its virtual identity record, so a rejoin of the same
device gets the same virtual light and HomeKit accessory.

### Layout

The layout stays owner-owned and keeps placement, names, scene and Sync
scales, `enabled`, `syncRole` and calibration. Its rows reference registry IDs.
The six rows are no longer mandatory; the device limit follows the registry.

The screen becomes its own layout object (position and diagonal). Today the
`hue-monitor` row position doubles as the screen position. Migration copies
that position to the screen object.

Calibration is allowed on every Govee row, not only `govee-left` and
`govee-right`.

### Sync: sampler and outputs

Sync already separates sampling from output. The virtual area samples a stable
screen perimeter (`screen-perimeter-v1`), and `sync_projection` maps each
light's placement onto that perimeter. This design keeps that separation and
makes the output side generic:

- **Sampler.** The virtual Entertainment area carries the perimeter capture
  channels. Its capture positions do not depend on which lights exist. For the
  migrated installation, the area, its lights and its channels stay exactly as
  they are (14 or 18 channels).
- **Outputs.** For every enrolled light with `syncRole: sync`, the profile
  holds one perimeter sample per zone and one visibility gain per zone. A
  basic-tier Govee light and a single-channel Hue light have one zone; a
  segmented Govee light has its model's segment count; a Hue gradient light has
  its channel count.

Because outputs are projected, the ten-light area limit applies only to the
sampler, not to the number of driven lights.

Enrolling or removing a light never changes the membership or channels of an
existing sampler area. A light enrolled later is an output-only light for Sync.
It still joins the virtual room, so Hue app scenes and power address it, because
scenes use the room and not the Entertainment area. This keeps the Sync Box
pairing untouched.

A new installation still needs a sampler area. The sampler's lights must be
virtual lights that exist in the Hue app. Proposed rule: when the area is first
created, it uses the enrolled lights (at most ten) as members and distributes
the perimeter capture positions over their channels. This rule needs a live
check with the Sync Box before it is final. Only a new installation creates the
area, so the rule and its check belong to the first-run setup (#59); stage 4
creates the virtual lights and keeps an existing area unchanged. Removing a light that is a
sampler member removes its physical output but keeps its virtual light in the
area until the owner rebuilds the area (a later setup-flow action, #59).

### Zone geometry

A light's zones need geometry, not only a point:

- A Govee model's table entry holds its length, zone order and base offset (for
  H6076: vertical, seven segments, 136 cm span, 60 cm base). The layout row's
  existing `orientation` sets the direction.
- A light marked as screen-mounted (the migrated `hue-monitor` strip) spreads its
  zones along the screen edge, as today.
- A Hue gradient light without a known geometry spreads its channels evenly
  along its `orientation` around its position. A basic-tier or single-channel
  light uses its position only.

### Govee support tiers

A capability table in code lists each model with tested values: protocol,
segment count, frame rate, packet limits and geometry (the current H6076
values). Tiers:

- **Segmented**: models in the table. Segmented Sync and gradient scenes. Today
  only H6076.
- **Basic**: any other Govee model that answers the LAN scan. One color for the
  whole light through `colorwc` plus brightness and power, with a rate that is
  safe for a non-streaming command path. Sync gives it the projected color of
  its placement center.

A model moves to the segmented tier only after a live test adds it to the table.
The H6076 constants that are now spread over `govee_segments`,
`sync_projection`, `live_preview`, `live_adapter` and `video_output` move into
its table entry.

### Hue lights

The native profile becomes a list of lights instead of four roles. Each light
has its registry ID, its native light and service UUIDs and its ordered native
channels. The area UUID stays. Migration converts the current four-light,
twelve-channel profile with the same UUIDs and channel order. #60 later derives
this profile from the bridge.

The native relay maps each enrolled Hue light's channels to projected colors and
keeps the current packet bytes for the migrated set.

Before #60, a new Hue light is enrolled from the physical area that the existing
pairing already uses. The owner adds the light to that area in the Hue app. The
core then reads the area's light, service and channel resources with the
existing Hue credentials and creates the profile entry. A Hue light outside that
area cannot be enrolled for Sync until #60. #60 keeps pairing and the area
choice.

### Virtual bridge

The bootstrap creates one virtual light for each enrolled light. Existing
lights keep their `id_v1`, UUID and model. A new light gets the next free
`id_v1` and a UUID seeded from its registry ID. Model choice per light: a
basic-tier Govee light is a color bulb, a segmented Govee light is a gradient
light, a Hue light mirrors the native light type.

Scenes from the Hue app address virtual lights. Scene palette assignment
(`scene_play_intent`) uses the registry instead of slots 1-6.

### Power, HomeKit and the plugin

The power owner, power feedback, Sync Off and `/homekit` API use light IDs
instead of `left`/`right`. Every Govee light can have a HomeKit power accessory.
The plugin keeps accessory UUIDs from the Govee device ID, so existing
accessories stay. The core cannot edit the Homebridge config, so the plugin
addresses lights by the Govee device ID that every lamp entry already has. The
core resolves it through the registry. The `role` field is no longer needed:
the plugin ignores it, and existing configs keep working without an edit.
Getting the lamp list from the core belongs to #62.

### Light Studio

The lamp list comes from the registry and the layout. The owner can enroll a
discovered supported light and remove an enrolled one. Enrollment needs the
light to pass the binding rules at that moment. A light that later goes offline
stays enrolled and shows as unavailable. The six seed lamps
disappear from the UI code. Identify and calibration work for every enrolled
light.

### Migration

The adapter runs one idempotent step at start, before any output. All core
containers mount the same state directory. The six migrated virtual lights need
no read of the diyHue state: their `id_v1` values (1-6) and identity seeds are
fixed constants today. The bridge reads the registry; until the registry exists,
it keeps creating the current six lights, so start order does not matter.

1. If the registry exists, do nothing.
2. Build the registry from the live targets file, the native profile, the
   layout and the bridge's virtual lights.
3. Convert the layout to the new version (screen object, registry IDs).
4. Convert the native profile to the list format.
5. Write each file atomically. Keep a pre-migration copy in the existing backup
   set.
6. Fail closed with a clear error if an input is inconsistent. Never guess.

The old formats are not read after this step. The live targets file and its
configuration field remain only as a migration input; after the registry exists,
the configuration no longer requires them. `stage` and `activate` already
snapshot state, so `rollback` restores the pre-migration files with the old
release.

## Stages

Each stage is a separate PR, verified live on the NAS before it is offered.

1. Registry and migration. All layers read the registry, but behavior is
   identical. Golden tests: the Sync profile signature, native packets and Govee
   frames for the current layout do not change.
2. Screen object and generic projection: per-light zones from placement for
   any number of lights, still with the current six.
3. Govee: any number of lights, capability table and tiers, per-light power and
   HomeKit, plugin update. Until stage 4, a newly enrolled Govee light works in
   Sync, power and HomeKit, but not in Hue app scenes.
4. Virtual bridge: virtual lights per registry entry, scene mapping, sampler
   area rule for new installations (live Sync Box check).
5. Hue: list-based native profile and generic relay. 5a: the profile becomes a
   list keyed by registry id, and the relay, proxies, adapter routes, status,
   Sync Off, identify, calibration, Light Studio and the gateway address native
   lights by registry id, with the migrated packet bytes unchanged. 5b: enroll
   a Hue light from the paired area, its projection and its virtual light.
6. Light Studio: enroll and remove lights; no fixed seed.

## Verification

- Offline: golden tests for stage 1, migration tests with the current state
  shapes, tests for 0, 1 and more Govee and Hue lights.
- Live on the owner's NAS: scene, PS5 Sync, Stop and HomeKit power after every
  stage. The owner has two H6076 lights, four native Hue lights and one more
  discovered Hue strip. The basic tier is checked by forcing an H6076 to the
  basic tier. Enrolling the extra Hue strip checks a new light end to end.

## Left to the builder

- The registry file name and JSON shape, and the new layout version number.
- How the adapter tracks a Govee IP change through discovery.
- The exact basic-tier command rate, from Govee LAN API limits and a live test.
- Whether a cap on driven lights is needed, from measured adapter load.
- Internal module boundaries for the generic projection.
