# Direct physical-output pilot

## Current status

The owner confirmed that both selected Govee changed colors through Vapor wave
Play in the official Hue app. The owner also confirmed that Stop ended cycling.
The initial live run still showed small color steps.

Physical output is now stopped at the owner's request. Offline tests and
virtual checks may continue. Do not remove the output hold or start hardware
controllers until the owner explicitly permits testing again.

The latest transport and Play lifecycle fixes pass offline tests and review.
Their physical smoothness has not been re-tested under the hold.

## Path

```text
Hue app -> reference bridge scene engine -> coalescing output queue
        -> authenticated local HTTP -> our host adapter -> pinned Govee LAN output
```

The bridge sends each target and its `transitiontime` in 100 ms units.
Vapor wave used a 4.7-second transition in the observed run. The host renders
these transitions at a 20 Hz target rate. It does not infer a scene phase from
the genuine Hue light's stored palette.

The two slots retain their existing test resource IDs and room. Only slot 1
maps to left and slot 2 maps to right. The host configuration contains exactly
the two previously confirmed Govee IDs and a 0.65 multiplier for each side.
No genuine Hue bridge key is copied into the container or pilot target file.

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
A mocked transport/render integration test exercises this gate at 50 ms
intervals. Hardware pacing after this change remains unverified.

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
- The old palette follower is stopped to prevent competing commands.
- NAS deployment, a Homebridge package, monitor-strip proxying, and HDMI Sync
  are outside this pilot.

## Resume gate

`state/live-output-hold` blocks adapter startup before network setup. It also
forces the bridge to simulation mode, even if the live flag is true.

After explicit owner permission, the next hardware check must cover Play,
Stop, static recall, brightness, off/on recovery, and command pacing.
Retain the pinned IDs, 0.65 calibration, and the identity freshness limit.
Do not run the old palette follower alongside the pilot.
