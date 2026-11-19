# Govee Signe gradient mode

This is an explicit extension of the six-light desk setup. It does not deploy
to NAS or replace Apple Home/Homebridge accessories.

## Mode and topology

Set `PROBE_GOVEE_GRADIENT=true` on both the bridge container and host adapter.
With it unset, the original 14-channel, whole-color mode remains the default.
The two existing virtual Govee retain their names, Hue IDs and room membership.
Their advertised profile becomes `915005987201` (Signe gradient floor), with
three scene gradient points and three Entertainment segments per lamp.

| Source channels | Destination |
| --- | --- |
| 0, 14, 15 | Left Govee bottom, middle, top |
| 1, 16, 17 | Right Govee bottom, middle, top |
| 2–10 | Existing nine native strip segments |
| 11–13 | Existing native Play mapping |

Native channel IDs and calibration remain unchanged. Gradient mode requires
HueStream v2, already observed from this Sync Box; v1 is rejected to avoid
ambiguous multi-gradient addresses. Bridge and host reject incomplete or
wrong-mode frames. The source has eighteen channels and six logical lamps.

## Physical output

The three Hue colors interpolate over seven H6076 segment addresses. `ptReal`
encodes a list of seven 20-byte color frames in one UDP datagram; every frame
contains a single segment bit and XOR checksum. Brightness is a separate,
deduplicated command; both datagrams count against the per-device pacing.
`max_gradient_packets_per_second` optionally adds a separate zoned datagram
budget; it does not reduce the configured legacy whole-color packet budget.
The selected older right controller uses 2/s for zoned output and 10/s for
legacy color. Seven segment frames in one datagram increase controller work.
At 10 zoned datagrams/s the right controller lost fresh identity/status proof
(0/25 eligible samples). After a quiet interval, 2/s gave 25/25 eligible
samples and the owner accepted video response and smoothness. These are local
measurements and settings, not a claimed vendor specification.

Existing fresh-on, identity/route verification, Sync priority, transitions and
Stop restoration also apply to segment output. Production output never sends
power commands. Segment colors are optimistic: LAN status does not provide
independent color readback for every zone.

Reference protocol: [govee-local-api](https://github.com/Galorhallen/govee-local-api),
commit `886fc569168be474d727c80ba56ade4d7237a58b`, `SegmentColorMessages` and
the H6076 capability entry. No additional package or Govee account is needed.

The owner physically confirmed simultaneous red bottom, green middle and blue
top on both selected H6076, and a reversed batched pattern on the left. This
confirms static zoned output/order. The owner also confirmed the Hue gradient
editor on the left and smooth zoned Vapor wave animation separately on both
lamps. The owner accepted zoned video response and right-lamp smoothness on
current PS5 content at the measured budget, and Stop returning all six lights
to smooth gradient Vapor wave. Optical latency, named game/HDR/Xbox/refresh-rate
combinations and prolonged soak remain unmeasured or not separately accepted.

## DreamView streaming during Sync

While Sync streams, zoned Govee output uses DreamView (`razer`) frames instead
of `ptReal`. One short packet carries the seven segment colors, bottom to top,
and the lamp blends them. Frame layout follows the OpenRGB Govee LAN controller:
`BB 00 <size> <command> <data> <xor>`, base64 in `pt`. `B1 01` turns streaming
on and `B1 00` off, and `B0 01 <count> <rgb...>` is a gradient frame.

- Rate: 30 frames/s per lamp by default (`max_stream_frames_per_second`). It
  replaces the ptReal budget only while streaming, and an unchanged frame is
  not repeated. With a new color in every frame both selected H6076 answered
  most status polls at 30 frames/s; at 35 the left one lost a quarter of
  them. See `evidence/dreamview-status-gaps.json`.
- The mode is sent at stream start, after a frame gap close to the lamp's
  one-minute timeout, after a verified Off and after a long status loss. It is
  switched off before any scene color, because the lamp ignores other color
  commands while it is on.
- The right lamp answers only about half of the `scan` requests. A scan
  proves which device answers at an address, so the adapter holds that proof
  for 5 s; another device's scan at the address withdraws the route at once.
  Control still needs a native status at most 0.75 s old.
- A DreamView packet cannot switch an off lamp on, so frames continue for up
  to 1.5 s after the last verified On while status replies are missing. A
  verified Off stops them at once, and a brightness change waits for a fresh
  status. Light Studio and the Sync health report keep the last verified
  reading for 2 s, so a short miss shows no warning and a longer loss shows
  "not responding".
- Brightness is sent only when it changes. The frame cadence holds when a
  render tick comes slightly late.
- A new frame wakes the gateway outbox and the adapter video worker at once.
  The adapter sends the Govee frame right after it arrives.

## Per-lamp calibration

Two H6076 lamps can render the same command differently. A target profile may
carry `color_gains` (red, green, blue; each above 0 and at most 1) and
`brightness_exponent` (above 0 and at most 1; the lamp gets
`100 * (b / 100) ** exponent`). Gains keep a colour's peak channel. The adapter
applies them to DreamView frames, ptReal segments, `colorwc` RGB and brightness
just before the transport, and converts statuses back, so the controllers, Sync
health and Light Studio keep logical values. A lamp without these fields is
unchanged. On 2026-11-19 the owner matched the left lamp to the right one with
`[0.9, 1, 0.9]` and `0.487` (left 60 looked like right 35, left 33 like right
10); see `docs/design/2026-11-19-govee-device-calibration.md`.

Live on the Mac with the PS5 through the Sync Box: ~50 frames/s reached the
adapter and ~20 frames per lamp per second reached each Govee light, with no
rejected sends or missed status. The owner confirmed that the Govee lights now
keep up with the native Hue lights without flicker.

The left lamp has a slower Wi-Fi path than the right one: median status round
trip 107 ms against 16 ms; ping minimum 40 ms against 4 ms. A small left/right
offset therefore remains. This is local measurement, not a vendor rate.

## Public API compatibility and phone startup

The pinned reference emulator exposes several incomplete public resources.
The probe normalizes bridge entertainment identity, room/home grouped-light
ownership (GET and Group SSE), and phantom bridge-home device services. Root
collections exclude the vendor-only resource, which contains an integration
key. Unconfigured geolocation omits empty sun-time data. Light responses keep
nullable mirek consistent with mirek_valid, provide complete inactive timed
effects and omit the legacy v1 colorloop name from v2 effects. These changes
are covered by isolated tests and live API checks.

During testing Hue hung at startup under iPhone Mirroring, including a
legacy-profile control run and a run with the emulator stopped. The owner
reported recovery after opening Hue directly on the unlocked phone without
Mirroring; Home and Sync were subsequently observed working through Mirroring.
The causal role of individual API repairs in that recovery was not isolated.
Do not describe the startup behavior as a proven Signe-specific defect or fix.

## Isolated probe

Stop the host adapter before running `segment_probe.py --targets PATH --side
left|right`. The probe refuses an active adapter, verifies the selected ID and
H6076 model, requires the lamp to already be on, and restores its previous
whole-lamp color in `finally` if fresh-on verification still permits it.
`--hold` keeps the pattern until interruption (maximum ten minutes),
`--reverse` reverses zone order and `--batch-only` sends one datagram.
Other fixture power isolation is a separate owner-authorized test action,
never an implicit behavior of this probe or the running adapter.


A later confirmed-power test uses a Left 4-datagram/s zonal profile with the
existing Right 2/s profile. This is a device-specific setting, not a vendor
limit. The owner subsequently reported that Left works as expected during Play;
no optical timing measurement is implied. Native coverage and three bounded Off
cases improved compared with the uncapped Left control; see
`evidence/confirmed-power-live.json`. Seven zones and the 0.65 calibration remain.
