# homebridge-lights-can-communicate

Homebridge platform `LightsCanCommunicate` for the two desk Govee H6076 lamps.
It replaces the version-specific overlay of `@homebridge-plugins/homebridge-govee`
(see `../experiments/homebridge-power`). Design:
`../docs/design/2026-11-17-homebridge-lights-can-communicate.md`.

## How it works

- Each lamp is published with the UUID the Govee plugin used
  (`hap.uuid.generate(deviceId)`), an AccessoryInformation service and one
  Lightbulb without subtype: On, Brightness, Hue, Saturation,
  ColorTemperature and adaptive lighting. On the same bridge identity,
  HAP-NodeJS therefore resolves the same AID and IIDs, and Apple Home keeps
  rooms, scenes and automations.
- The plugin never talks to a lamp. Every HomeKit write is sent to the core
  (`POST /homekit`); the core executes power itself, keeps the color writer
  closed until native On is confirmed, and applies Home brightness and color
  only while no scene or Sync owns the lamp. A write that the core cannot
  confirm is reported to HomeKit as a communication failure.
- The plugin polls `GET /homekit` every second and shows the core's intent.
  Changes made outside HomeKit (for example in the Govee app) are adopted by
  the core after a short settling time. After five seconds without the core,
  the lamps show "No Response".

## Configuration

`config.json` platform entry (no secrets):

```json
{
  "platform": "LightsCanCommunicate",
  "lamps": [
    {"role": "left", "deviceId": "<Govee device ID>", "name": "Mykola office L"},
    {"role": "right", "deviceId": "<Govee device ID>", "name": "Mykola office R"}
  ],
  "_bridge": {"username": "<the Govee child bridge username>", "pin": "...", "port": 0}
}
```

`lights-can-communicate.json` in the Homebridge storage directory, mode 0600:

```json
{"version": 1, "endpoint": "http://<core address>:19880", "token": "<power-owner credential>"}
```

The core runs with `adapter.power_authority: "homekit"` (or
`--power-authority homekit`). In that mode the legacy `/power-owner` route is
closed, so the overlay can no longer obtain color permission.

## Migration (requires owner authorization)

1. Rehearse on copies, read-only:
   `node homebridge/tools/rehearse-identity.mjs <IdentifierCache copy> <lamps.json>`
   must report `known: true` and empty `added`/`unused` for both lamps.
2. Back up `config.json`, `accessories/` and `persist/`.
3. Stop Homebridge. In `config.json`, give the Govee platform a new
   `_bridge` username and port and add both lamps to `lightDevices` with
   `"ignoreDevice": true` (Govee plugin 11.40.0 option); add
   the `LightsCanCommunicate` platform with the old Govee `_bridge` values.
   Never start the old bridge identity without the two lamps: HAP-NodeJS
   expires unused identifiers when it saves the cache.
4. Switch the core to the HomeKit authority and restart it.
5. Start Homebridge. Check Home Off/On, brightness, color and a Home scene for
   both lamps, and Hue App On/Off. Add Table Lamp from the new bridge once.
6. Reinstall the original Govee release to remove the overlay, then delete
   `smart-lighting-power.json` and `smart-lighting-power-state.json`.

Rollback: stop Homebridge, restore the backed-up `config.json`, switch the
core back to `power_authority: "owner"`, start both. The Govee plugin regains
the lamps with the same UUIDs on the old bridge.

## Tests

`sh scripts/setup-dev.sh` installs `@homebridge/hap-nodejs` 2.3.0 (the release
in Homebridge 2.5.0); `node --test tests/homebridge_plugin.test.mjs` runs the
identity, setter, polling and credential contracts.
