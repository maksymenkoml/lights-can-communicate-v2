# Homebridge power coordination pilot

Prepared locally; **not installed on the NAS**. Existing HomeKit accessories,
bridge identity, services, pairing and scenes remain unchanged. Hardware testing
and any NAS deployment remain separate acceptance gates.

## Why this exists

The right H6076's native LAN power state changed from Off to On after exactly one
seven-zone black `ptReal` packet. This measures logical power, not light output
from the black frame. The existing independent color writer can race
an Apple Home Off even with fresh LAN status. The original plugin itself notes
implicit power-on from color commands and delays its own Off by 400 ms.

This overlay wraps only the main-light On setter in the exact installed
[`@homebridge-plugins/homebridge-govee` 11.39.1 source](https://github.com/homebridge-plugins/homebridge-govee/blob/v11.39.1/lib/device/light.js).
Its unchanged original
handler still performs the actual power command. The wrapper first pauses the
pilot's color writer, and grants color after successful On. UUID generation and
service creation are not replaced. The third accessory is passed through using
the original setter without coordination.

## Local protocol

- Opt in to the adapter with `--power-owner-token-file <private-token-file>`.
  The owner credential must differ from the existing adapter credential.
- `POST /power-owner` uses `X-Power-Owner-Token`, not the stream/command key.
  Its strict envelope contains `session`, `sequence`, Unix `issued_at` and a
  `states` object restricted to boolean `left`/`right`. No physical ID, IP or
  arbitrary command is accepted.
- Color permission expires per role after two seconds; fresh owner updates are
  sent every 350 ms. Messages outside the half-second clock tolerance are
  rejected. Keep both hosts' system clocks synchronized.
- An Off acknowledgement waits for in-flight sends. The same gate protects
  scene and video writers; native identity/on checks and packet pacing remain.
  Permission changes invalidate color deduplication, including rapid Off/On.
- If the coordinator cannot be reached, the wrapper waits out possible old
  grants plus clock/drain margin before still performing the original Off.
  Explicit rejection, conflicting authority or malformed acknowledgements
  require a successful pause or a HomeKit error; they cannot use that fallback.
- Power intent persists separately from native observations. A restart with a
  stale Home cache cannot overwrite a saved Off. Shutdown cancels queued or
  not-yet-started power writes and stops permission renewal.

The authoritative owner in this slice is the existing Homebridge On setter.
Power changes made through other independent applications/controllers are not
intercepted. Do not claim that installing the local Python gate alone integrates
the current NAS power writer.

## Prepare, do not install

```sh
python3 -B experiments/homebridge-power/prepare_overlay.py \
  --source /path/to/exact-public-11.39.1-source \
  --output /new/local/overlay-directory
```

The builder verifies package identity/version and the original `light.js` SHA256,
refuses existing output, retains the upstream license and produces a manifest.
Only three runtime files are in the overlay: the patched light handler and two
new modules under `lib/smart-lighting-power/`. Source, package metadata, platform
factory and Homebridge state are not changed by preparation. The upstream MIT
license is included in each prepared artifact.

The hook is optional when its private config is absent. A configured hook reads
`smart-lighting-power.json` from `api.user.storagePath()`:

```json
{
  "version": 1,
  "endpoint": "http://192.168.10.117:19880/power-owner",
  "token": "<dedicated private owner credential>"
}
```

Keep this file mode 0600. Its separate `smart-lighting-power-state.json` contains
only owner intent; it does not replace Homebridge's accessory cache. Neither file
belongs in Git or logs. The exact installed source must pass the manifest guard
before any installation. A plugin upgrade invalidates this overlay until checked
again; this is a compatibility experiment, not the final Homebridge package.

## Acceptance and hold

Verified locally: gate ordering, stale-grant expiry, replay, isolated credentials,
concurrent Off/On, shutdown, state failures, and real loopback Node-to-Python
renderer flow with simulated implicit power-on firmware. The exact patched
upstream class also imports in an isolated public-source copy.

Pending: coordinated Off/On on the actual lamps, exact NAS-file preflight,
explicit approval for installation/child-bridge restart, and acceptance with the
existing Home scenes. No NAS source/config has been changed.

The owner sleep hold is `experiments/hue-bridge/state/live-output-hold`. It must
remain until the owner explicitly resumes physical testing. Keep the live adapter
and test bridge stopped during this hold. Source-only checks may use isolated
containers with `--network none` and no private state mounts. On resume, use the
documented bridge recreation path to avoid stale Docker UDP publication.
