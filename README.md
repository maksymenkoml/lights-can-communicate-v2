# Smart lighting prototype

Copy the stored palette of one Philips Hue light to one or two Govee LAN
controllers. The runtime uses Python 3.10+ and its standard library only.
This is a local experiment, not a background service.

When Hue reports dynamic scene playback, Govee animate locally through that
palette. This matches the scene's colors, not the instantaneous Hue colors or
animation phase.

## Connect

1. Enable **LAN Control** for each Govee device in Govee Home. The computer,
   lamps, and Hue Bridge must be on a network that allows local communication.
2. Run `python3 smart_lighting.py discover`.
3. Run `python3 smart_lighting.py pair` and press the physical Hue Bridge button.
   If discovery cannot find the bridge, use `pair --bridge 192.168.x.x`.
4. Run `python3 smart_lighting.py lights` and choose the monitor strip's ID.
5. Select the source and targets:

   ```sh
   python3 smart_lighting.py configure --light HUE-LIGHT-UUID --left LEFT-IP --right RIGHT-IP
   ```

   For two bars sharing a single controller/IP, use `--govee IP` instead;
   both bars receive the center color. The basic LAN API cannot independently
   address segments or bars attached to the same controller.

## Try it

```sh
python3 smart_lighting.py sync --once --dry-run
python3 smart_lighting.py sync --once
python3 smart_lighting.py sync
```

Pick a scene in Hue and leave the last command running to follow changes.
Ctrl+C stops the program; the lights keep their last state. `status` reads
the selected Govee controllers without changing them.

If Govee look brighter on the wall, save a separate brightness multiplier for
each side. Stop the running sync, calibrate, and start sync again:

```sh
python3 smart_lighting.py calibrate --left 0.5 --right 0.5
python3 smart_lighting.py sync
```

With a 0.5 multiplier, Hue at 80% gives Govee 40%. Each side can use a different
value greater than 0 and up to 1; 1 restores full brightness copying. For a
single controller use `calibrate --govee 0.5`. Calibration is stored locally
alongside the device selection and applies to static and animated scenes.
The current desk's two Govee were visually balanced at 0.5 each.

For static scenes, the left Govee gets the first stored Hue gradient point and the right Govee
gets the last. For a solid Hue color they get the same color. Hue brightness
is copied as an integer percentage after the per-device multiplier (minimum 1%). The program never sends
power commands: on/off stays with Homebridge and its existing automations.
Off Govee controllers are skipped. A missed UDP status reply skips
only that controller and retries on the next cycle; other lamps keep updating.
No color or brightness is sent to a controller whose state could not be read. The one-shot
command still returns an error if a controller is unreachable. When Hue is off,
palette updates pause. Active white temperature takes priority over stale
color/gradient fields. If the physical gradient direction is reversed, swap
the left/right IP selection.

The program polls Hue every two seconds. For a static scene it sends updates
only when the target changes, plus a periodic refresh because UDP delivery is
not guaranteed. For dynamic scenes it interpolates between palette colors
with eased transitions, targeting five color frames per second. Hue's speed
setting controls approximate pacing; its internal timing is not known.
Each animation frame rechecks Govee power with a short timeout. Unknown/off
controllers are skipped; brightness is sent during the main polling cycle.
Changing the palette or speed restarts the local cycle; stopping Play restores
static following. Hue Sync pauses both static and animated updates.
`--once` prints the Govee status readback; a missing reply returns an error.
Readback alone does not prove the colors look right: compare the physical lamps.

## Limits

- Hue Sync's video/music/entertainment stream is not mirrored. When the Hue
  light reports streaming mode, updates pause automatically; Govee keep their
  last colors. Following resumes when streaming ends. Some Hue Sync desktop
  presets may also be streamed: try a scene saved on the Hue Bridge instead.
  Pause/resume is unit-tested; console/Sync Box behavior needs a physical test.
- During a real Vapor wave Play check, all ten consecutive Hue API samples
  reported dynamic playback but returned identical color/gradient values.
  The local animation is an approximation using those stored palette colors;
  it cannot reproduce the live Hue phase or eliminate phase drift.
- Govee gets one color per controller at a time. Segment effects and exact dynamic
  scene reproduction are outside this experiment.
- RGB conversion is an approximation; different LEDs, walls, and brightness
  mean a visual match may need later calibration.
- No automatic startup, cloud control, UI, changes to the Hue light, or changes
  to Homebridge/HomeKit/Matter configuration. Concurrent Homebridge color or
  brightness automations can overwrite the prototype and vice versa; use one
  source for palette and brightness while testing. A power-off racing with a color write
  still needs physical verification on the actual lamps.
- Pairing and discovered addresses live in ignored `.lighting.json`, written
  with owner-only permissions. Do not share this file. Local Hue HTTPS uses a
  scoped self-signed-certificate exception; use a trusted home network.

## Development

```sh
uvx --cache-dir .ruff_cache/uv ruff check .
python3 -B -m unittest discover -s tests -v
git config core.hooksPath .githooks
```

The pre-push hook runs cached Ruff offline and unit tests. Run the Ruff command
above once to populate the cache. `uv` is only needed for development;
it is not needed to run the prototype.

Protocol references: [Govee LAN API](https://app-h5.govee.com/user-manual/wlan-guide),
[Hue API V2](https://developers.meethue.com/new-hue-api/).
