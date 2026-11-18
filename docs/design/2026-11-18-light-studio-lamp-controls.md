# Light Studio lamp controls

## Why

The redesigned Light Studio (#28) shows four lamp controls that the runtime
cannot execute yet: switching a lamp off while Sync runs, blinking a lamp to
find it, naming the six desk lamps, and a brightness correction that differs
between Sync and the lamp's own light. The owner chose (2026-11-18):

- these controls cover the six role lamps only;
- connecting discovered lamps to Sync is a separate later design and PR;
- a lamp name set in Light Studio is used everywhere: Light Studio, the
  virtual Hue bridge, HomeKit and the physical Hue lights.

This work starts after #32 (native Hue power) merges and reuses its power path.

## Two configurations

Light Studio edits two independent configurations: with Sync (the Sync view)
and without Sync (the room view, scenes from the Hue app). Each has its own
brightness correction per lamp, its own fade zone below the desk, and its own
switch that turns the fade on or off. The owner decided this on 2026-11-18.

- With Sync: `scale`, `syncZone`, `syncFalloff` (default on, as before).
- Without Sync: `sceneScale` (default `scale`), `sceneZone` (default automatic),
  `sceneFalloff` (default off, so scenes look as before until the owner turns
  it on).
- Fade off means every gain is 1: zones keep full brightness and Govee
  segments sample at their centres.
- Scene output applies the scene fade per Govee segment (bottom to top, as in
  Sync), to each Play's dimming and to the strip's dimming (mean of its nine
  anchor gains). Hue dimming never goes below 0.1 %, because scene writes may
  not switch power.
- "Own light" means only "outside the Sync composition". Scene paths (scene
  controller, Hue scene proxies, the bridge's scene forwarding) include every
  present lamp; only Sync paths exclude own-light lamps.

## Layout (version 5)

Version 4 files load as version 5; writes must use version 5.

- Top level: `syncZone` (existing), `sceneZone` (same format, optional),
  `syncFalloff` and `sceneFalloff` (booleans, optional, defaults above).
- Rows: `sceneScale` 0-100 (optional, default `scale`).
- Role rows (lamp controls, below): `syncRole` gains `"off"`; `name` 1-35
  characters.

`STATE_SCHEMAS["desk_layout"]` becomes 5.

## Off during Sync

- A Sync session starts when `VideoOutput` becomes active and ends when it is
  released (explicit stop or grace elapsed).
- At the start, the adapter records each `"off"` lamp's power and switches it
  off; at the end it restores the recorded power. A lamp that was already off
  stays off; a lamp the owner switches on or off during the session keeps the
  owner's choice at the end.
- Govee: a separate Sync hold in the power authority (`HomeKitPower` /
  `PowerOwner`). The hold is not HomeKit intent, is never adopted as intent,
  and closes the colour writer while it is active. HomeKit keeps showing the
  owner's intent.
- Native Hue: `HueStripProxy.set_power` from #32. The role is outside the
  composition, so the `safe()` fence does not require it to be on.
- A restart during a session clears holds and restores recorded power from a
  small state file, so a crash never leaves a lamp off.

## Blink

- Setup Studio `POST /api/identify {lamp}` → adapter `POST /identify {role}`
  (adapter token). Only an owner click triggers it; nothing blinks on its own,
  and tests use fakes.
- Govee: two short brightness dips, only while the lamp is on and the colour
  writer is open; otherwise the request is refused.
- Native Hue: the Hue v2 `identify` action on the pinned light. The client
  allowlist gains this one field.
- The card's Blink tile also blinks the drawing, so the owner sees which lamp
  was asked.

## Names everywhere

- Light Studio: the card name is editable for role lamps and saved as `name`.
- Virtual Hue bridge: the bridge reads layout names and renames its virtual
  lights (the Hue app and Sync box see them). Resource IDs never change.
- HomeKit: `GET /homekit` returns each lamp's name; the plugin updates the
  accessory `Name`/`ConfiguredName`. UUIDs, AIDs and IIDs never change, so
  rooms, scenes and automations survive. Apple Home may keep a name that the
  owner edited in Home.
- Physical Hue lights: the adapter writes `metadata.name` to the pinned light
  on the owner's Hue bridge, only when the name changes in Light Studio.

## Light Studio views

- Each view edits its own configuration: correction rail, fade zone and the
  fade switch on the zone chip. A zone that is off stays selectable as a faint
  outline, so it can be turned on again.
- The Sync view without running Sync previews Sync from a demo picture on
  every Sync lamp; the room view shows only the lamps' own light.

## Not in this PR

- Connecting discovered lamps (dynamic outputs).
- Any change to running services, NAS installs or pairing; live testing uses
  a staged release activated by the owner.
