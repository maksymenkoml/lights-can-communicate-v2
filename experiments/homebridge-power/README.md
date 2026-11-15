# Homebridge power coordination pilot

Installed as an approved compatibility overlay on the NAS on 2026-11-14.
Existing HomeKit accessories, bridge identity, services and pairing are retained.
Automatic Apple Home Off/On has live API coverage; the owner confirmed physical
Right Lamp Off. Existing scene execution and visual On acceptance remain separate
acceptance gates.

## Why this exists

The right H6076's native LAN power state changed from Off to On after exactly one
seven-zone black `ptReal` packet. This measures logical power, not light output
from the black frame. The existing independent color writer can race
an Apple Home Off even with fresh LAN status. The original plugin itself notes
implicit power-on from color commands and delays its own Off by 400 ms.

This overlay coordinates the main-light On setter and the existing instance color writers in the exact installed
[`@homebridge-plugins/homebridge-govee` 11.39.1 source](https://github.com/homebridge-plugins/homebridge-govee/blob/v11.39.1/lib/device/light.js).
Its unchanged original
handler still performs the actual power command. The wrapper first pauses the
pilot's color writer, and grants color after identity-verified native On confirmation. UUID generation and
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

The owner subsequently confirmed physical Right Lamp Off through Apple Home.
Pending: visual On confirmation, equivalent live Left Lamp coverage, and existing
Home scene execution. No re-pairing was needed;
scene definitions and the accessory cache were not edited.

## Virtual Hue state feedback

The virtual bridge now polls the existing authenticated adapter status and
projects fresh NAS intent into only slots 1/2. A Home Off changes the virtual
light and emits Hue light and room/zone/bridge-home events. The feedback does not
call a physical power setter or stop a dynamic scene worker. Home On resubmits
the current color target through the existing color adapter. Repeated On
heartbeats do not override a Hue-local Off; unavailable/expired authority does
not invent a physical power change. While Home is Off, scene On values are
masked in the model, Hue API and events.

This is Home-to-Hue state feedback, not a Hue-to-Home power command path. Power
commands originating in Hue are not translated into the NAS owner's On setter
by this slice. Brightness/color feedback from unrelated controllers is also
outside its scope.

Fourteen unit tests cover role validation, expired/malformed snapshots, repeated and
rapid revisions, Off masking, animation preservation and static color resume.
Scene/power replies queue one deferred Off confirmation because the mobile app
can apply the scene's saved On after the immediate event. The correction window
starts from the latest successful reply; fresh Home On cancels it.

Eight additional journal tests cover independent readers, immutable messages,
bounded replay, legacy queue clearing, keepalive and reconnect snapshots.
`tests/check_power_feedback_upstream.py` additionally runs six isolated checks
against the pinned actual diyHue classes. Run it inside the pinned image with
`--network none`, mounting source-only copies of `bootstrap.py`, `bridge_output.py`,
`gradient_profile.py`, `hue_resource_compat.py`, `power_feedback.py`, and `hue_events.py` under
`/probe`, plus the checker itself. Never mount private `state` for these checks.
Its fixture includes the actual EntertainmentConfiguration class: it must not
receive room grouped-light events. It also exercises the real Flask SSE route,
authorization, replay, and concurrent geometry publication during a snapshot.

After recreating only the live reference bridge, six read-only samples showed
the existing NAS Right Off matching both virtual state and the actual Hue v2
API. Left remained On and feedback reported no errors; see
`../hue-bridge/evidence/hue-power-feedback-live.json`.

## iPhone verification and SSE repair

Actual iPhone Mirroring checks exposed the pinned SSE transport losing events:
its broker clears a shared list after 0.3 seconds, but a reader can sleep for
0.4 seconds. A Home On appended in that interval is removed before the reader
sees it. Different batches can also reuse an SSE ID. Public-source deterministic
reproduction confirmed both conditions independently of devices.

The compatibility layer now journals each message before Hue object modules
import their publisher aliases. It still calls the original publisher and keeps
the legacy broker intact. The app route reads a bounded journal with a separate
cursor per subscriber and unique IDs. It uses the existing v2 authorization,
including Sync Box client-key matching. Both the real Hue app and Box received
HTTP 200 on the authenticated event route after installation.

New connections/history gaps receive current virtual lights, grouped state,
Entertainment and scene state. The cursor and creation time are captured before
reading resources, so concurrent layout events are replayed. This restores the
selected lamps' state; it does not claim recovery of missed arbitrary resource
deletions after journal overflow.

After the repair, the agent observed Home On/Off in the actual Hue mobile UI,
including Right On during Play, both Off during Play, scene recall while Right
remained Off, and Stop. Final native and virtual states matched Left On/Right Off,
with Play stopped. No extra owner clicks were required.

Physical Left Off coverage is **partial**: one attempt retained native On despite
closed color permission; Apple Home later reflected On again. A repeated ordinary
Home Off succeeded in eight fresh native readbacks. The cause of that missed
physical write is unresolved and is not hidden by the UI result. The wrapper/NAS
power writer was not changed by this SSE slice. See
`../hue-bridge/evidence/iphone-power-feedback.json`.

The owner sleep hold is `experiments/hue-bridge/state/live-output-hold`. It must
remain until the owner explicitly resumes physical testing. Keep the live adapter
and test bridge stopped during this hold. Source-only checks may use isolated
containers with `--network none` and no private state mounts. On resume, use the
documented bridge recreation path to avoid stale Docker UDP publication.


## Confirmed native power and SDK writer coordination

The NAS hook enables optional native readback confirmation for the selected two
accessories. The restricted owner response includes an observer epoch and fresh,
identity-verified `on`/`sample` observations for each selected role. A scan refresh
cannot manufacture a new native sample. On requires two distinct fresh samples
before scene/video permission; Off retains its closed gate throughout a bounded
settling/retry window. Retries use the same existing platform power sender and
cannot be authorized by unknown identity. A definitive failure reports a Home
error instead of declaring success from a UDP send callback. Coordinator outage
retains independent explicit Home power operation; reconnect needs fresh native
On proofs before restoring colors.

Debounced SDK brightness, color, temperature and custom-scene writes share the
same local power intent. Off invalidates pending debounce keys and waits for
already-started writers. Paired Home scene properties wait for On; explicit scene
On and brightness-zero Off retain their meanings. Failed scene On can be retried.
Async-local epochs also fence delayed SDK characteristic updates on the existing
main and custom-scene services. Services, UUIDs, accessory cache, scene definitions
and pairing remain unchanged; the third device bypasses all coordination.

The guarded upgrader accepts `--previous-manifest` to verify the exact installed
overlay and update only its two runtime modules. It keeps independent backups,
checks claimed inodes, publishes without clobbering concurrent changes, and rolls
back conditionally. Existing private configuration must already be mode 0600;
updates do not rewrite it or restart services.

Offline checks include 63 Node tests, 258 Python tests and real Node-to-Python
loopback. A separate source-hash-checked harness runs three tests against the
actual pinned upstream brightness/CT method bodies. Linux Node 22 passes the
same Node suite. Live native telemetry and actual Home UI results are recorded
separately from optical acceptance; an uncapped Left zonal Play Off still failed
after installation, so that hardware edge is not declared resolved.


Heartbeat dispatch is serialized between periodic renewal and verification.
Permission masks, sequence and timestamps are generated at actual dispatch, so a
queued old mask cannot re-enable a newer Off. The strict replay guard and stale
response fences remain intact. Matching fresh native telemetry refreshes the
existing HAP characteristic outside active power operations; unknown readbacks
and unconfirmed On cannot clear a fault. A Home UI write-error badge can remain
locally cached until the app refreshes even when fresh reads are correct.


## Latest live boundary

See [confirmed-power-live.json](../hue-bridge/evidence/confirmed-power-live.json).
The uncapped Left scene stream had 64.4% fresh identity/native coverage during
one On capture; a 4/s zonal test profile reached 99.3%. Three subsequent Left Off
cases at 4/s converged in 4.78, 5.41 and 5.00 seconds, including independent Left
Off while Right continued rendering. There was no additional native confirmation
failure in those cases. The uncapped control case took 9.76 seconds and exceeded
the bounded confirmation window; it remains a recorded failure.

With the host adapter deliberately paused, ordinary Home Right Off and Left On
still used the original NAS power sender. The expected unavailable warning was
recorded, both lamps remained Off after Off/reconnect, and seven-zone animation
resumed after On/reconnect with Right still Off. The first recovery capture was
already granted, so it does not independently prove the two-proof timing.

Final Home, virtual Hue API and fresh native telemetry agree on both Govee Off.
The owner calibration remains 0.65; the current Left 4/s setting is a test profile
whose optical smoothness still needs acceptance. iPhone Mirroring was blocked by
an in-use phone, and HDMI Sync was not rerun in this slice. Existing whole-Home
scenes were not invoked because they can affect the excluded third lamp. No
scene definition, accessory identity or pairing file was edited.
