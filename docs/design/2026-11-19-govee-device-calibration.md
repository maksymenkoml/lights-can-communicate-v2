# Govee device calibration

Decided by the owner on 2026-11-19.

## Intent

The two H6076 floor lamps render the same command differently. At brightness 100
and white 255,255,255 they match. Below that the left lamp is darker, and its
green is weaker, so the cream whites of Sync video look pink on it. The owner saw
the lamp light "bluer/pinker" than the Light Studio preview. A Light Studio
correction cannot fix it, because it multiplies by one number and the difference
depends on the level.

## Measurements (owner's eye, other lamps off, 2026-11-19)

Right lamp as sent; left lamp adjusted until both looked the same:

| Right lamp | Left lamp that matched |
|---|---|
| 255,255,255 at 100 | 230,255,230 at 100 (unadjusted also matched) |
| 255,235,207 at 35 | 249,255,202 at 60 |
| 255,235,207 at 10 | 249,255,202 at 33 |

A relative gamma of about 2 applied to the colour channels made the left lamp
purple, so the colour difference is a channel gain, not a curve.

## Goals

- An optional per-lamp calibration in the target profile (`live-targets.json`):
  `color_gains` (three numbers 0 < g <= 1, red, green, blue) and
  `brightness_exponent` (sent brightness = 100 * (b / 100) ** exponent).
  Left: `[0.9, 1, 0.9]` and `0.487` (ln 0.6 / ln 0.35). Right: none.
- Colour gains keep the colour's peak channel: the scaled colour is multiplied
  back so its largest channel equals the original largest channel.
- Applied only at the transport boundary, to everything sent to the lamp:
  DreamView frames, ptReal segments, `colorwc` RGB (not colour temperature) and
  brightness. Statuses from the lamp are converted back, so every check above
  the transport (brightness resend, fresh-status rules, Sync health, Light
  Studio preview) keeps working in logical values.
- A lamp without calibration behaves exactly as before.

## Non-goals

- No Light Studio control for the calibration; it is device data, set in the
  profile.
- Power commands (`turn`) are not changed.
- The older standalone controller in `lights_can_communicate_v2.py` is not
  changed; the adapter is the runtime.

## Approaches

- **Chosen: calibrate at the transport boundary.** One wrapper around the Govee
  sender and one status decoder; the scene and Sync controllers and the preview
  stay in logical values.
- Calibrate inside `GoveeController`: rejected. Both controllers, the preview and
  the brightness resend check would each need to know the device values.
- Use the Light Studio correction: rejected. It cannot follow a level-dependent
  difference, and the colour gain is not a brightness.

## Left to the builder

- Validation limits for the profile fields; an invalid profile refuses to start.
- Rounding: brightness round-trips exactly for exponents up to 1.
