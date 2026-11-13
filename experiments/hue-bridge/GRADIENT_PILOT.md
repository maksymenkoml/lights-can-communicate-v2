# Govee Signe gradient pilot

This is an explicit extension of the six-light Mac pilot. It does not deploy
to NAS or replace Apple Home/Homebridge accessories.

## Mode and topology

Set `PROBE_GOVEE_GRADIENT=true` on both the bridge container and host adapter.
With it unset, the original 14-channel, whole-color pilot remains the default.
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
lamps. New eighteen-channel Sync acceptance and optical latency remain separate.

## Isolated probe

Stop the host adapter before running `segment_probe.py --targets PATH --side
left|right`. The probe refuses an active adapter, verifies the selected ID and
H6076 model, requires the lamp to already be on, and restores its previous
whole-lamp color in `finally` if fresh-on verification still permits it.
`--hold` keeps the pattern until interruption (maximum ten minutes),
`--reverse` reverses zone order and `--batch-only` sends one datagram.
Other fixture power isolation is a separate owner-authorized test action,
never an implicit behavior of this probe or the running pilot.
