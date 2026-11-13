# Animation pacing measurements

The same desk, Hue Vapor wave in dynamic Play, speed 0.881, and both Govee
brightness multipliers at 0.5 were used for both runs. Each run requested a
15-second window and finished at the next Hue read boundary.

| Controller | Baseline median gap | Updated median gap | Baseline p95 gap | Updated p95 gap | Baseline max channel step | Updated max channel step |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Left | 308.84 ms | 50.12 ms | 615.22 ms | 103.51 ms | 94 | 17 |
| Right | 315.83 ms | 50.12 ms | 540.07 ms | 99.15 ms | 66 | 18 |

The baseline was commit `bb25893`. Measurements wrap the actual application
functions: color-command send times, Hue request duration, and Govee status
query duration. Commands were sent to the physical lamps; these are host send
timestamps, not a photometric measurement of LED update times. Identical RGB
frames are deduplicated, so a command gap can also include unchanged/rounded
colors. The frame scheduler targets 20 Hz rather than promising 20 distinct
colors per second. Startup is included.

The largest recorded command gaps fell from 728.25/1073.47 ms to
451.11/396.58 ms. The remaining gaps are not evidence of perfectly continuous
LED output. The desk owner separately confirmed that Play transitions looked
smooth after the update.

Raw data contains only side labels, timestamps, counters, and pacing values;
bridge credentials and device addresses are excluded.

## Reproduce

Stop the normal sync before measuring: only one process should read the
Govee reply port. Enable Vapor wave Play and leave the scene unchanged.
From the repository root, run:

```sh
PYTHONPATH=. python3 -B archive/smoothing/tmp_measure_animation.py after
```

The archived helper is retained to reproduce the measurements. It reads the
ignored local device configuration and writes a sanitized JSON result. It
changes colors through the same sync path and never sends power commands.
Restart normal `sync` after the measurement finishes.
