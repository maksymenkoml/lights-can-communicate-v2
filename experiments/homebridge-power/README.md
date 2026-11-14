# Homebridge power coordination pilot

Installed as an approved compatibility overlay on the NAS on 2026-11-14.
Existing HomeKit accessories, bridge identity, services and pairing are retained.
Automatic Apple Home Off/On has live API coverage; existing scene execution and
visual acceptance of this NAS-backed path remain separate acceptance gates.

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

`apply-overlay.mjs` is read-only by default. With explicit `--apply`, it requires
writable original source and backup storage on the same filesystem. It creates
the module directory and private config without replacing existing paths, then
claims the actual original light inode before publishing the replacement with
a no-clobber link. The original snapshot and claimed inode remain in a private
backup. Concurrent changes are preserved and stop installation; ambiguous
rollback state is retained for inspection. It never changes the package,
platform factory, Homebridge configuration, accessory cache or pairing files,
and never restarts a service itself.

The installer restricts and verifies the actual config inode as mode 0600 before
writing credential bytes. This matters on Synology, where inherited permissions
overrode the requested creation mode during the first installation. That live
file was corrected and verified before the child bridge was restarted. A
filesystem that cannot enforce the mode fails before activation; an already
installed config with broader permissions is rejected for inspection.

## Acceptance and hold

Verified locally: gate ordering, stale-grant expiry, replay, isolated credentials,
concurrent Off/On, shutdown, state failures, and real loopback Node-to-Python
renderer flow with simulated implicit power-on firmware. The exact patched
upstream class also imports in an isolated public-source copy.

The right H6076 also passed a real coordinated Off/On check using a local owner
harness before the existing Apple Home action. Eight fresh identity-confirmed
readbacks stayed Off while the other five lights reacted to the game (owner
confirmed). On restored seven-zone video after initial warmup, also owner
confirmed. This proves the local ordering independently of the NAS setter.
Installed NAS source/version checksums matched the prepared original exactly.

The approved installation changed only the three overlay runtime files and added
the private owner config. The original factory checksum remained unchanged;
actual Node 22 syntax checks and patched upstream module import passed. Only the
Govee child bridge was restarted. The backup remains on the NAS; the temporary
transfer bundle was removed after installed checksums and config mode were
verified.

With the temporary local owner harness stopped, the NAS now renews both grants.
A plain Right Lamp Off in Apple Home captured Off intent automatically: six
fresh identity-confirmed native samples stayed Off and color permission stayed
closed. Plain On restored both grants, seven zones and advancing video frames
in six samples. Existing native-cache safety pauses occurred during Off sampling
and recovered; this is not evidence of uninterrupted video. See
`../hue-bridge/evidence/nas-power-live.json`.

Pending: owner visual confirmation of this automatic NAS path, equivalent live
Left Lamp coverage, and existing Home scene execution. No re-pairing was needed;
scene definitions and the accessory cache were not edited.

The owner sleep hold is `experiments/hue-bridge/state/live-output-hold`. It must
remain until the owner explicitly resumes physical testing. Keep the live adapter
and test bridge stopped during this hold. Source-only checks may use isolated
containers with `--network none` and no private state mounts. On resume, use the
documented bridge recreation path to avoid stale Docker UDP publication.
