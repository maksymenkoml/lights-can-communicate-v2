# Light Studio lamp controls

## Why

The redesigned Light Studio (#28) shows four lamp controls that the runtime
cannot execute yet: switching a lamp off while Sync runs, blinking a lamp to
find it, naming the six desk lamps, and a brightness correction that differs
between Sync and the lamp's own light. The owner chose (2026-11-18):

- one PR for these four controls, for the six role lamps only;
- connecting discovered lamps to Sync is a separate later design and PR;
- a lamp name set in Light Studio is used everywhere: Light Studio, the
  virtual Hue bridge, HomeKit and the physical Hue lights.

This work starts after #32 (native Hue power) merges and reuses its power path.

## Layout (version 5)

Role rows gain optional fields; version 4 files load as version 5.

- `syncRole`: `"sync"` (default, not stored), `"own"`, `"off"`. `"off"` is
  outside the Sync composition like `"own"`, and the adapter also switches the
  lamp off for the Sync session.
- `name`: 1-35 characters for role rows. Missing means the built-in name.
- `sceneScale`: 0-100, the correction outside Sync. Missing means `scale`.
  `scale` stays the correction during Sync.

`STATE_SCHEMAS["desk_layout"]` becomes 5. Writes must use version 5.

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

## Correction per Sync state

- The scene controller and Hue proxies use `sceneScale` outside Sync; the Sync
  controller and the relay use `scale`. The proxy changes scale only at a mode
  change, so a Sync toggle causes one restore write, not repeated writes.
- Light Studio shows the correction for the current view: Sync view edits
  `scale`, the room view edits `sceneScale`.

## Not in this PR

- Connecting discovered lamps (dynamic outputs).
- Any change to running services, NAS installs or pairing; live testing uses
  a staged release activated by the owner.
