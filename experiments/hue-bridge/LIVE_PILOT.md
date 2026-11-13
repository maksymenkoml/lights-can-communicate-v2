# Direct physical-output pilot

## Current status

The pilot now also includes the owner's confirmed monitor strip and three
under-desk Hue Play lamps. The virtual room has six lights. A separate
`Vapor wave DESK` scene supplies five gradient points to the strip, the first
point to both left lights, the last point to both right lights, and the middle
point to Hue Play Center. The existing gallery scene remains available.

The owner subsequently set Hue Play Center to a 0.50 brightness multiplier,
relative to the strip's unchanged 1.00 multiplier. The other two Play lamps
remain at 1.00 and both Govee at 0.65. Calibration is applied once to each
incoming scene brightness, including dynamic updates; native on recovery
reuses the already calibrated desired state.
Settled native readback after applying the change was 39.13% Center against
77.87% strip (native rounding); see `evidence/live-center-calibration.json`.

Static desk recall reached both Govee at 51% and all four native Hue lights at
77.87% readback (requested 78%, rounded through v1 brightness). Native Play
color readback matched the three selected palette positions. Strip readback
contained five gradient points. The owner subsequently confirmed that all six
lights animate smoothly with the intended colors, and that Stop works.
See `evidence/live-desk-static.json`.

## Desk acceptance and Sync priority

The owner confirmed six-light Play, colors, Stop, and the Center brightness
correction. See `evidence/desk-owner-acceptance.json` for those visual checks.

During the console test, the owner reported that Sync worked, disconnected
with Connecting, and reconnected; similar disconnects had occurred before.
The following observation does not identify the cause of that disconnect.

After reconnect, all four native lights reported streaming. Across a
7-second observation their proxy write counters stayed at 61 each and the
Govee command counter stayed at 3460, despite virtual Play remaining active.
The pilot yielded physical output to Sync in this observed window.

After the owner stopped Sync, all four lights reported normal mode. Across
the next observation each native proxy wrote two updates and the Govee
counter advanced by 278, with no rejected Govee sends. This confirms command
delivery resumed. The owner separately confirmed that all lights visually
returned as before and the shared animation continues. See
`evidence/desk-sync-observation.json`.

Govee video synchronization is not implemented: they hold their last color
during observed streaming. Correction of native Hue brightness is applied to
our scene commands; it is not injected into Sync Box streaming.

The owner resumed hardware testing on 2026-11-13. The selected two-lamp pilot
was restarted with the saved 0.65 calibration. The owner confirmed smooth
Vapor wave Play after the continuous receiver update.

Static scene colors and brightness matched native readback. Stop produced no
new control commands after the previous worker sleep. A virtual brightness
of 40% produced native 26% on both lamps. Static recall restored native 51%.

The owner also tested the left lamp through Apple Home. Native off was
confirmed, 40 rendering frames were blocked, and no new left color command
was sent during the off observation. After the owner turned it on, the
adapter sent a new target and native readback matched the static orange
color and calibrated 51% brightness. The owner reported the expected color.
The adapter sent no power commands. The right lamp remained on.

The hold mechanism remains available for later pauses. Never remove an
active owner hold without explicit permission.

## Path

```text
Hue app -> reference bridge scene engine -> coalescing output queue
        -> authenticated local HTTP -> our host adapter -> pinned Govee LAN output
                                                      -> four pinned native Hue lights
```

The bridge sends each target and its `transitiontime` in 100 ms units.
Vapor wave used a 4.7-second transition in the observed run. The host renders
these transitions at a 20 Hz target rate. It does not infer a scene phase from
the genuine Hue light's stored palette.

The two Govee slots retain their existing test resource IDs and room. Slot 1
maps to left and slot 2 maps to right. The host configuration contains exactly
the two previously confirmed Govee IDs and a 0.65 multiplier for each side.
No genuine Hue bridge key is copied into the container or pilot target file.

Opt-in slot 3 maps to the genuine gradient strip. Opt-in slots 4, 5, and 6
map to the explicitly confirmed Hue Play Left, Center, and Right. Each role
has one fixed native v2 resource ID. Request bodies cannot choose another ID.
The native client's write whitelist excludes power and all unrelated fields.

The pinned reference engine stores the current group state when loading a
group scene, so expanding a room can add default actions to older scenes.
The new DESK scene explicitly defines all six actions and keeps the gallery
palette and speed. Use DESK for this composition. Original scene IDs and
their saved palette are retained; exact old action membership is not promised.

The controller sends only color and brightness commands. Native off or stale
state blocks output. Virtual off stops output without changing physical power.
Existing Homebridge scenes continue to own on/off during this pilot.

## Smoothness investigation

The initial host reader used sequential scan/status transactions. The measured
12-second Play window showed these command timings:

| Side | Median gap | Largest gap | Largest channel step | Stale-state skips | Rejected sends |
| --- | ---: | ---: | ---: | ---: | ---: |
| Left | 50.60 ms | 1550.60 ms | 63 | 132 | 22 |
| Right | 50.36 ms | 900.70 ms | 37 | 24 | 25 |

These are host command timings, not optical measurements. Native snapshots
expired while the old reader waited. The 0.75-second identity limit was kept.

The candidate transport now keeps one UDP receiver open. It polls identity
and status without per-request waits. It processes all observed scan replies,
including replies from other peers. Ambiguous identities block output.
Native off revokes the write gate before deferred batch publication.
Only a fresh identity plus a fresh native-on status can authorize output.
Status refresh alone cannot extend an old identity proof.

The renderer still uses `smart_lighting.send_to_target` as its only write gate.
A mocked transport/render integration test exercises this gate at 50 ms.
After owner resume, the same two lamps completed this 14-second live window:

| Side | Median gap | p95 gap | Largest gap | Largest channel step | State skips / rejected sends |
| --- | ---: | ---: | ---: | ---: | ---: |
| Left | 50.01 ms | 53.75 ms | 150.25 ms | 3 | 0 / 0 |
| Right | 50.01 ms | 53.77 ms | 100.93 ms | 3 | 0 / 0 |

Identical RGB frames are deduplicated. These command gaps do not measure LED
update times. The owner separately confirmed that transitions looked smooth.

## Play lifecycle

The pinned upstream starts a new worker for every Play. Its shared status
flag could revive an old worker after a fast Stop followed by Play.

The wrapper now reserves a generation when the scene creates the thread.
Static and Stop commands cancel active and deferred generations. Each worker
uses a guarded view of dynamics and its own palette copy. A canceled worker
cannot change virtual state or forward physical targets.

Offline tests also used the actual pinned `Scene.activate` and
`Light.dynamicScenePlay` methods with a fake lamp and deferred threads.
The newer worker emitted its target; the older pending worker was rejected.
These tests made no physical network requests.

## Runtime boundaries

- `bridge_output.py` coalesces per-side targets and preserves their duration.
- `live_adapter.py` validates the token, command shape, age, session, and sequence.
- `pilot_transport.py` owns UDP 4002 and publishes fresh pinned-ID state.
- API key and adapter token values are never included in status output.
- Native status checks and rendering use separate threads.
- Each Hue proxy uses native transitions and checks its power and streaming
  mode before writing. Observed off is never changed by the adapter. Accepted
  color is retained for owner-operated on recovery. Streaming or unavailable
  state in any selected Hue pauses Govee rendering. This is a polling guard,
  not proof of HDMI Sync compatibility.
- The old palette follower is stopped to prevent competing commands.
- NAS deployment, a Homebridge package, and Govee video synchronization
  are outside this pilot. The Sync priority observation above covers one
  live streaming and resume path, not general Sync Box reliability.

## Optional native Hue output

Set `PROBE_HUE_STRIP=true` and `PROBE_HUE_PLAY=true` in the private probe
environment, then recreate only this project's bridge container. Start the
host adapter with `--hue-config` pointing to the owner's private real-bridge
configuration and `--hue-play`. Do not copy that configuration into Docker.
The flags default off. Without them, the original two-Govee scope remains.
The three confirmed native Play IDs are intentionally pinned for this pilot;
generic discovery and configuration belong to later plugin work.

## Resume gate

`state/live-output-hold` blocks adapter startup before network setup. It also
forces the bridge to simulation mode, even if the live flag is true.

The owner explicitly resumed testing. Play, Stop, static recall, brightness,
command pacing, and owner-operated left off/on recovery passed the resumed
live checks. Right off/on was not separately cycled in this run.

Retain the pinned IDs, 0.65 calibration, and the identity freshness limit.
Do not run the old palette follower alongside the pilot. Any future owner
hold must remain active until the owner explicitly permits testing again.
