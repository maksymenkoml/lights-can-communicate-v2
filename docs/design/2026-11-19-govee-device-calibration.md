# Govee colour calibration in Light Studio

Decided by the owner on 2026-11-19.

## Intent

The two H6076 floor lamps render the same command differently from each other
and from the Hue lamps. The owner saw the Govee light "bluer" than the Light
Studio preview, and found the Hue lamps closest to the television. Matching was
first done by hand in a chat session; the owner wants to do it in Light Studio
instead: "це ж має конфігуруватись в браузері, а не я з тобою в чаті".

## What the chat session measured (owner's eye, other lamps dark)

| Right lamp (as sent) | Left lamp that matched |
|---|---|
| 255,255,255 at 100 | 230,255,230 at 100 (unadjusted also matched) |
| 255,235,207 at 35 | 249,255,202 at 60 |
| 255,235,207 at 10 | 249,255,202 at 33 |

- A relative gamma on the colour channels made the left lamp purple, so the
  colour difference is a channel gain; the brightness difference is a curve
  (`100 * (b / 100) ** 0.487` matched at 35 and 10).
- With three Hue Plays showing 255,235,207 as the reference, the right lamp
  matched at 255,200,104: gains `[1, 0.85, 0.5]`. The left lamp's total is
  about `[0.9, 0.85, 0.45]`. In Sync both Govee then matched the Hue lamps with
  the existing Sync correction 35.

## Goals

- A calibration mode for each Govee lamp in Light Studio (variant A, chosen
  from three mock-ups): the lamp card gets "Calibrate color" above "Remove from
  layout". The mode shows a card beside the lamp, on the side away from the
  reference so both stay visible:
  - title "Calibrate <lamp>", one line "Make it look like the reference. Other
    lamps are off.";
  - Reference: a select of the other lamps (default: the nearest Hue Play);
  - Test light: Bright / Dim;
  - sliders Warmth (Cooler-Warmer), Tint (Greener-Pinker), Low light
    (Darker-Brighter, hint "Check it on Dim");
  - Cancel and Save.
  - The scene dims everything except the two lamps, tags them "Calibrating" and
    "Reference", draws a dashed line between them and shows both lit with the
    test white. Handles, chips and guides are hidden.
- While the mode is open, the calibrated lamp and the reference show the same
  test white (255,235,207): Bright at full level, Dim at a low level. Every
  other lamp is dark. A slider change shows on the lamp at once, unsaved.
- Save stores the values in the layout; Cancel keeps the old ones. Both end the
  session, and the lamps return to their scene.
- The adapter applies the saved calibration to everything sent to the lamp:
  DreamView frames, ptReal segments, `colorwc` RGB (not colour temperature) and
  brightness, in scenes and in Sync. Colour gains keep a colour's peak channel.
  Statuses are converted back, so every check above the transport keeps
  logical values. Light Studio's preview keeps showing the intended colour.
- Today's values carry over, so both lamps start calibrated and the sliders
  show that position.

## Non-goals and anti-goals

- No calibration for Hue lamps: they manage colour themselves and are the
  reference.
- The overall brightness stays the existing correction; calibration covers
  colour and the low-level curve only.
- The mode must not change power state: HomeKit owns power, so "dark" means a
  black frame for Govee and minimum dimming for Hue, never Off.
- A lost browser must not leave lamps in the test state.

## Constraints

- While Sync runs, the Hue Entertainment stream holds the Hue lamps and REST
  colour writes do not show, so the reference cannot be set. Calibration is
  available only while Sync is not running; the card item is disabled with the
  tooltip "Turn Sync off to calibrate".
- Layout writes need If-Match and the current version.
- Standard library only; no new runtime dependencies.

## Approaches

- **Chosen: store slider positions in the layout; the adapter derives gains and
  the curve and runs a calibration session.** One mapping (positions to gains
  and exponent) in the adapter; Light Studio stores and shows positions; the
  adapter already reads the layout live.
- Store raw gains in the layout: rejected. Light Studio would need the inverse
  mapping to place the sliders, and hand-made values could have no slider
  position.
- Keep the calibration in the adapter's private profile (`live-targets.json`,
  the first version of PR #50): rejected by the owner; it is not editable in the
  browser.
- Pause Sync during calibration instead of refusing: rejected. The adapter
  would have to stop the Hue Entertainment relay while the Sync Box keeps
  sending, which is a larger and riskier change.

## Design

### Layout (version 6)

- Govee role rows (`govee-left`, `govee-right`) accept an optional
  `calibration`: `{"warmth": -100..100, "tint": -100..100, "lowLight": 0..100}`
  (integers). Missing means all zero. Version 5 files load as version 6;
  writes use version 6; `STATE_SCHEMAS["desk_layout"]` becomes 6.

### Mapping (adapter, one function)

With `w = warmth / 100`, `t = tint / 100`, `l = lowLight / 100`:

- warmer (`w > 0`): blue x `1 - 0.6 w`, green x `1 - 0.18 w`; cooler (`w < 0`):
  red x `1 + 0.6 w`, green x `1 + 0.18 w`.
- greener (`t < 0`): red and blue x `1 + 0.3 t`; pinker (`t > 0`): green x
  `1 - 0.3 t`.
- gains are divided by their largest value; exponent = `1 - 0.7 l`.
- Today's values: right `{warmth: 83}`, left `{warmth: 83, tint: -33,
  lowLight: 74}` (gains about `[1, 0.85, 0.5]` and `[0.9, 0.85, 0.45]`,
  exponent 0.482).

### Carrying today's values over (builder's call)

- The layout is the only source. The adapter no longer reads `color_gains` or
  `brightness_exponent` from the target profile; a profile that still has them
  refuses to start, so a lamp is never calibrated twice.
- The version 5 to 6 migration adds no calibration (missing means zero), so
  fixtures and fresh installs stay neutral.
- On the owner's Mac the values are written once through the Light Studio API,
  and the two profile fields are removed in the same step. The Mac runtime is
  owner-authorized for live changes; the NAS is not touched.

### Calibration session (adapter)

- `POST /calibration` (adapter, token) with `{"side", "reference", "level",
  "calibration"}` starts or updates a session; `POST /calibration/end` ends it.
  Light Studio's server forwards `/api/calibration` like `/api/identify`.
- Refused while Sync is active, while the side is off or not freshly observed,
  or while another session or a blink runs.
- Sync wins (builder's call): when Sync starts during a session, the adapter
  ends the session at once and restores the lamps; the next renewal is refused,
  and Light Studio closes the card with "Calibration stopped: Sync started".
  Unsaved slider values are dropped.
- Lamps involved (builder's call): only the six role lamps under adapter
  control. Discovered or unbound rows are left alone. The reference select
  lists role lamps; a lamp that is off is shown disabled with "(off)". If the
  calibrated lamp or the reference turns off during a session, the session
  ends the same way ("Calibration stopped: <lamp> is off"). Dark lamps that are
  off stay off; nothing is switched on.
- While a session runs, the scene controller's writes to the involved lamps
  are rejected (like Identify's fence) and resent after the session. The
  calibrated Govee gets the test white through the candidate calibration; the
  reference gets the same colour (Hue: xy and dimming; Govee: through its saved
  calibration); other lamps get dark colours.
- Each update renews a 5 s lease; Light Studio renews it every 2 s while the
  mode is open. Without renewal the adapter ends the session itself.

### Transport calibration

`bridge/govee_calibration.py` (from PR #50) wraps the Govee sender and decodes
statuses; it reads the saved calibration from the layout store per send, and
the session can override it for its side.

## Left to the builder

- Test levels for Bright and Dim per lamp type.
- Session ordering details and the HTTP error bodies.
- Exact card styling within the existing card rules.
