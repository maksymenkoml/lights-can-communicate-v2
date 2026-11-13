# Entertainment relay pilot

This opt-in experiment adds a video path to the existing six-light desk pilot.
It is not a packaged Homebridge plugin or a NAS deployment.

## Verified scope

| Check | Result | Evidence |
| --- | --- | --- |
| Offline codec, channel mapping, arbitration and lifecycle | PASS | Full 130-test suite and Ruff |
| Encrypted v2 reception through published Docker UDP 2100 | PASS | 30 synthetic packets sent and received; host video output disabled during that check |
| Genuine Hue Bridge DTLS handshake | PASS | Four selected lights, 12 native channels, no video frames sent; owned stream released |
| Sync Box registration and switch to virtual area | PENDING | Push-link registration requires the owner to press the box button |
| Sync Box real frame format and timing | PENDING | Synthetic reception does not prove box compatibility |
| Physical video colors, latency, calibration and black | PENDING | Requires actual source and owner observation |
| Stop, scene return, owner off and reconnect with video | PENDING | Covered offline; six-light ordinary Sync handoff was tested separately |

See `evidence/entertainment-receiver-selftest.json` and
`evidence/entertainment-native-handshake.json`. The receiver-only check used
the old host adapter without a `/stream` endpoint; synthetic frames could not
reach physical outputs. The new host video path is now enabled and idle.
Do not repeat that synthetic test against physical output without explicitly
selecting the intended test colors and checking power state.

## Path and mapping

```text
HDMI Sync Box -> virtual DTLS receiver -> authenticated /stream frames
             -> host video lease -> two pinned Govee LAN targets
                                 -> genuine Hue Entertainment DTLS stream
```

The virtual `Desk Sync PILOT` area has six logical lights and 14 channels.
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
RGB intensity; the other Hue receive 1.00. Physical brightness matching under
video still requires observation. Black frames send black color, never power.
The source supports the fixed desk layout in HueStream v1/v2 RGB and xyY;
only the synthetic v2 RGB path has been exercised over encrypted transport.

## Ownership and lifecycle

The source receiver validates complete frame membership and forwards the
latest frame at a 20 Hz target cadence. Frames have session, sequence and
wall-clock freshness checks. The host expires a frame lease after 0.5 seconds
without fresh accepted frames; a stopped or expired session cannot revive
with delayed packets. A new source stream receives a new session ID.

Virtual scenes may continue running while video owns physical output. Scene
REST writes and Govee scene rendering are gated during a video lease and
pending native cleanup. After video ends, each Hue proxy explicitly restores
its accepted scene target and Govee scene deduplication is cleared.

Start requires the four selected native lights to be observed on, healthy,
fresh and normal. Owner off or unavailable state during our stream releases
it and blocks that source session; restarting the source is required. A
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

The Sync Box 8K was discovered through `_huesync._tcp` as the owner's HSB2,
firmware 2.5.4 / API level 10. Its local API token belongs in ignored host-only
`.sync-box.json`, outside Docker mounts. Before changing its Bridge, archive
its current Hue association and execution state privately and prepare a
working route back to the genuine bridge. Never print tokens or credentials,
reset the box, alter HDMI wiring, or launch a game.
