# Light Studio lamp card and calibration

Decided by the owner on 2026-11-19. Supersedes the separate calibration card
("variant A") in `2026-11-19-govee-device-calibration.md`; the adapter-side
calibration (layout v6, mapping, transport) stays as decided there.

## Intent

The owner found the lamp chip, the small "⋯" button, the lamp menu and the
calibration mode "primitive" and inconsistent, "as if different people with
different ideas of Light Studio made them". The card and the calibration must
read as one design, and brightness correction belongs with the calibration.

## Goals

- **Chips.** Every object chip (lamps, desk, monitor, fade zone) has the same
  shape: 24 px high, 5 px radius, the existing chip colours. A lamp chip is one
  button: name, status, and a chevron (down when closed, up when open); a click
  anywhere on it opens or closes the card. A Play lamp keeps its rotate button
  inside the chip, after a thin divider. A double click on a lamp opens its
  card.
- **Card** (chosen from three mock-ups: "list, slide in"). Under the header
  (editable name, model and zones, the Sync blocker alert when there is one):
  the "During Sync" choice, then rows of one style (icon, label, value on the
  right): Blink; "Calibrate ›" with the lamp's correction for the current
  settings view (`85%`, plus ` · color` for a calibrated Govee lamp); and,
  after a divider, "Remove from layout" in red. The separate "Reset correction"
  row is gone.
- **Placement.** The card opens beside the selection (lamp, handles and chip)
  on the side of the nearer screen edge, where it covers less of the scene; it
  uses the other side when it does not fit. It follows its object when the
  scene re-renders or resizes.
- **Calibrate page.** "Calibrate ›" slides the card to a second page in the
  same card, with "‹" back, title "Calibrate" and the lamp name:
  - Compare with: a select of the other role lamps (default: the nearest lit
    Hue Play), off lamps disabled with "(off)";
  - Test light: Bright / Dim;
  - Brightness: the correction of the configuration shown by the toolbar's
    Sync switch, labelled "with Sync" or "without Sync", 0-100 %;
  - Govee lamps only: Warmth (Cooler-Warmer), Tint (Greener-Pinker), Low light
    (Darker-Brighter);
  - Reset (left; colour to zero, brightness to 100 %, unsaved), Cancel, Save.
  - Back, Cancel and Esc discard; Save stores brightness and colour in the
    layout. The page keeps the card's side; the card moves only when the
    reference lies on that side.
- **Scene while the page is open.** Everything except the lamp and its
  reference dims like a selection (22 %); handles and guides hide; the lamp's
  chip stays. The reference is lit and opaque, lights the wall, and carries
  the scene label "Compared with". The old floating card, boxed tags and the
  dashed link are gone.
- **Test light.** Both lamps show the test white (255,235,207) at the test
  level times their brightness correction for the shown configuration: the
  calibrated lamp with the slider value (unsaved), the reference with its saved
  correction. Colour and brightness can both be matched. Other role lamps are
  dark.
- **Correction rail removed.** The vertical correction rail, its knob and its
  `%` label leave the scene; correction is edited only on the Calibrate page.

## Builder's calls approved by the owner

- **During Sync** the page opens without Compare with and Test light. The
  Brightness slider (with Sync) changes the running Sync at once, as the rail
  did: each change is saved through the normal layout save. Cancel, back and
  Esc restore the value the page opened with. The colour sliders are replaced
  by "Turn Sync off to calibrate color".
- **Lamps the adapter does not drive** (found lamps, not connected lamps) get
  the page without the test: Brightness only, saved on Save.
- Hue lamps (strip and Plays) get the page with Brightness and the test; the
  adapter shows the test on a Hue lamp as on a Govee lamp.
- The work goes into PR #50.

## Non-goals and anti-goals

- Power never changes; dark means black or minimum dimming.
- No calibration of Hue colour.
- A lost browser must not leave lamps in the test state (5 s lease, as before).

## Adapter changes

- `POST /calibration` accepts `{"lamp", "reference", "level", "view",
  "correction"}` plus `"calibration"` for a Govee lamp (required) and never
  for a Hue lamp. `lamp` is any role lamp; `view` is `"sync"` or `"scene"`;
  `correction` is the candidate 0-100.
- Levels: the lamp gets `round(level * correction / 100)`, the reference
  `round(level * saved / 100)` with its saved correction for `view` (Sync
  `scale` or `sceneScale`); a lit level is at least 1 (Govee) or 0.5 % (Hue).
- A Hue lamp under test gets xy of the test white and the dimming; other Hue
  lamps get minimum dimming; the rest is unchanged from the Govee session.

## Left to the builder

- Animation timing (the page slide and the card move: 280 ms).
- Exact copy of disabled rows ("Turn Sync off" as the row value).
