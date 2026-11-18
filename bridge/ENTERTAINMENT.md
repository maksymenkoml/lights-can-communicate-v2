# Entertainment relay

This opt-in mode adds a video path to the existing six-light desk setup.
It is not a packaged Homebridge plugin or a NAS deployment.

## Verified scope

| Check | Result | Evidence |
| --- | --- | --- |
| Offline codec, channel mapping, arbitration and lifecycle | PASS | Full 139-test suite and Ruff |
| Encrypted v2 reception through published Docker UDP 2100 | PASS | 30 synthetic packets sent and received; host video output disabled during that check |
| Genuine Hue Bridge DTLS handshake | PASS | Four selected lights, 12 native channels, no video frames sent; owned stream released |
| Sync Box switch to virtual area | PASS | Official Hue app via iPhone Mirroring paired the box to the virtual bridge and its Desk Sync area (both under their earlier test-marked names) |
| Sync Box real frame format | PASS | Actual v2 RGB frames on all 14 source channels |
| Physical video response in PS5 menu | PASS | Owner confirmed both Govee react with Hue, acceptable smoothness and delay |
| PS5 gameplay | PASS | Owner launched the game and reported everything works great; title not recorded |
| Optical black/latency measurements | NOT MEASURED | Acceptance is visual, not an instrumented LED latency measurement |
| Video Stop and scene return | PASS | Native mode/command readback plus owner confirmed return to Vapor wave animation |
| Physical owner off during video / Xbox | NOT TESTED | Unit gates covered; these hardware paths were not separately cycled |

See `evidence/entertainment-receiver-selftest.json` and
`evidence/entertainment-native-handshake.json`. The receiver-only check used
the old host adapter without a `/stream` endpoint; synthetic frames could not
reach physical outputs. The host video path is enabled; the owner also accepted a PS5 game test. Menu acceptance and Stop/scene return are recorded in `evidence/entertainment-owner-acceptance.json`.
Do not repeat that synthetic test against physical output without explicitly
selecting the intended test colors and checking power state.

## Path and mapping

```text
HDMI Sync Box -> virtual DTLS receiver -> authenticated /stream frames
             -> host video lease -> two pinned Govee LAN targets
                                 -> genuine Hue Entertainment DTLS stream
```

The virtual `Desk Sync` area has six logical lights and 14 channels.
The monitor strip has nine Entertainment segments, independently of its
five-point scene palette. Channel order is explicit:

| Virtual channel | Role | Native channel |
| --- | --- | --- |
| 0 | Govee Left | LAN left only |
| 1 | Govee Right | LAN right only |
| 2–10 | Nine strip segments | 3–11 |
| 11 | Hue Play Left | 1 |
| 12 | Hue Play Center | 0 |
| 13 | Hue Play Right | 2 |

The source area uses the strip's native segment positions and the confirmed
Play positions. Native membership and the channel service/index layout are
validated before streaming. Any change fails closed rather than routing
colors to an unexpected light. No third Govee or unrelated Hue is allowed.

Govee retain 0.65 brightness calibration. Center Play receives 0.50 of incoming
RGB intensity; the other Hue receive 1.00. The owner accepted the composition in the PS5 menu. Black frames send black color, never power; physical black-scene acceptance remains separate.
The source supports the fixed desk layout in HueStream v1/v2 RGB and xyY;
v2 RGB has been exercised over encrypted transport with both a synthetic client and the actual Sync Box. Other encodings remain unit-tested only.

## Ownership and lifecycle

The source receiver validates complete frame membership and forwards the
latest frame at a 20 Hz target cadence. Frames have session, sequence and
wall-clock freshness checks. The host expires a frame lease after 0.5 seconds
without fresh accepted frames. A fresh, higher-sequence frame can resume a timed-out session; stale/replayed frames cannot. Replacing a session retires the previous one; explicit Stop and shutdown reject late frames. A new source stream receives a new session ID.

Virtual scenes may continue running while video owns physical output. Scene
REST writes and Govee scene rendering are gated during a video lease and
pending native cleanup. After video ends, each Hue proxy explicitly restores
its accepted scene target and Govee scene deduplication is cleared.

Start requires the four selected native lights to be observed on, healthy,
fresh and normal. Freshly confirmed owner off releases the stream and blocks that source session; restarting the source is required. Temporarily stale/unavailable native state suppresses output and retries after cleanup. After handshake, the controller waits for fresh native polls before output rather than treating the handshake delay as owner off. A
foreign native stream is never taken over or stopped. Native cleanup checks
ownership, retries failures, and retains a pending fence if confirmation is
unavailable. Adapter startup can release only a stale stream belonging to
its own dedicated application identity.

Orderly shutdown closes incoming HTTP, revokes the lease, joins native cleanup,
requests scene restoration while scene/status workers still run, and then
stops those workers. Failed cleanup retains the scene fence. A network failure
cannot guarantee immediate restoration; the next adapter startup retries
cleanup of its own stream.

## Local runtime

Set `PROBE_ENTERTAINMENT=true` alongside both native Hue proxy flags and
recreate only the existing test container. The area and receiver are seeded
by `bootstrap.py`; credentials and iPhone pairing remain in private state.

The host adapter adds `--hue-stream-config .lighting-stream.json` to the
existing `--hue-config`, `--hue-play`, targets and adapter-token arguments.
The ignored host-only 0600 stream file contains `bridge`, `hue_key`,
`client_key`, `application_id` and the pinned native `area_id`. Its application
UUID is obtained from the `/auth/v1` response header. That UUID is the native
v2 DTLS identity and the `active_streamer` ownership identifier; the application
key is used for REST authentication. These are not interchangeable.

The Sync Box 8K was discovered through `_huesync._tcp` as the owner's HSB2, firmware 2.5.4 / API level 10. Local token push-link did not complete and was not needed: the official Hue app changed the association from the genuine Hue Bridge / Mykola Office to the virtual bridge and its Desk Sync area (then under their earlier test-marked names). No `.sync-box.json` credential was created. The same Change Hue Bridge flow exposes the genuine Hue Bridge for returning to native-only Sync. No box reset or HDMI rewiring was performed; the owner launches all games.

## Older right-controller pacing

Both lamps identify as H6076, but the right lamp has older Wi-Fi hardware/firmware. The LAN switch was observed off, and Wi-Fi reapplication plus a complete owner-operated power/controller restart restored the correct pinned scan/status. Sparse requests and normal read-only polling stayed responsive. Color traffic at 20 Hz had substantially more identity gaps than 10 Hz in matched local tests; a persistent output socket alone did not remove those gaps. See `evidence/right-packet-pacing-comparison.json`. This is measured local behavior, not a claimed vendor rate limit.

The private target profile now sets `max_commands_per_second: 10` for the right lamp only. The renderer counts color and brightness packets together and coalesces to the latest target. Safety pauses/off/stale states invalidate deduplication but retain pacing debt. The left lamp keeps its existing cadence. Video carries intensity in RGB at a fixed calibrated 65% global brightness, reducing each changing video target to one color packet. Scene brightness still follows the scene with its accepted 0.65 multiplier.

The driver also probes a discovered unambiguous candidate IP for read-only identity/status before persisting a moved route; scan alone cannot authorize control. UDP send errors invalidate delivery and retry rather than terminating both lamps.
