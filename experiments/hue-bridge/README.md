# Hue app compatibility probe

Disposable reference experiment for the planned custom Homebridge plugin.
The default mode uses a pinned diyHue runtime with two simulated color lamps.
An explicit live-pilot mode can forward these two slots to our host adapter.
This is not the final Homebridge plugin. Upstream physical `set_light`
drivers remain blocked. Our separate adapter uses the pinned Govee allowlist.

**Owner resume, 2026-11-13:** the owner explicitly resumed hardware testing.
The sleep hold was released and the selected two-lamp pilot was restarted.
The hold mechanism remains available for later pauses. Do not remove a future
`state/live-output-hold` without explicit owner permission.

See [LIVE_PILOT.md](LIVE_PILOT.md) for scope, implementation, and evidence limits.

**Desk expansion:** an opt-in six-light pilot now adds the genuine monitor
strip and the owner's three confirmed Hue Play lamps through a host-side,
color-only proxy. The separate `Vapor wave DESK` scene aligns palette edges
and center; Govee retain 0.65 brightness calibration. Static native readback
is recorded in `evidence/live-desk-static.json`. The owner confirmed smooth
six-light Play, intended colors, Stop, and Center calibration. A live Sync
priority check observed paused writes during streaming and resumed commands
after Sync stopped. Visual resume and Sync Box reliability remain separate
checks. This remains a local reference experiment.

## Phase 1: simulated compatibility result

Tested on 2026-11-13 with the owner's installed official Philips Hue app on
iPhone. The exact application version was not recorded. The bridge recorded
the client label `Hue#iPhone` and user agent `Dart/3.13 (dart:io)`.

| Check | Result | Evidence boundary |
| --- | --- | --- |
| HTTP identity and HTTPS | PASS | Host API requests; separate test bridge ID |
| Hue API v2 lights and room | PASS | Two `dummy` lights and one room |
| iPhone pairing | PASS | Registered client and owner confirmation |
| Room shown in official Hue app | PASS | Owner saw the TEST room |
| Gallery Vapor wave creation | PASS | iPhone POSTed scene actions, metadata, palette, speed |
| Static scene recall | PASS | Successful iPhone recall requests |
| Play | PASS | Scene and both simulated lamps report `dynamic_palette` |
| Physical Govee output/calibration | NOT CONNECTED | Existing Python controller remains separate |
| Hue Sync / HDMI Sync Box | NOT TESTED | Not implied by room or scene compatibility |
| NAS deployment / Homebridge plugin | NOT IMPLEMENTED | This probe runs locally on the Mac |

The shipped metadata `1.67.0` / `1967054020` caused a mandatory firmware
update screen without a skip button. The probe's public BSB002 metadata was
changed to `1.78.0` / `1978545030`, matching the public values read from the
working genuine bridge. After restarting the iPhone app, the owner reached
the room and created/played Vapor wave. No actual firmware was installed and
the genuine bridge was not modified. Bridge identity remains separate.

This demonstrates the tested request paths, not complete Hue API parity.
Declared version metadata must remain consistent with implemented features.
Online version scraping is disabled to keep the experiment reproducible.

## Runtime

Reference image:

`diyhue/core@sha256:392c1c39a5b46168d1d6d8582abd266586bdd45d61fb3459fa95821c710ffcba`

The wrapper seeds `Govee Left TEST`, `Govee Right TEST`, and
`Probe Office TEST`. All seeded lights use the upstream `dummy` protocol.
It does not mount the existing `.lighting.json` or real bridge credentials.
Remote discovery and configured physical integrations are disabled.

Copy `.env.example` to `.env` and set the test host's actual LAN IP and MAC.
Use a distinct host identity, not the genuine Hue Bridge's MAC. The host
must have ports 80/443 TCP and 1900 UDP available. From this directory:

```sh
docker compose -p smart-lighting-hue-probe up -d
```

Credentials, pairing keys, certificate, state, and sanitized request logs
are generated under ignored `state/`. The directory uses mode 0700 and the
probe's secret files use mode 0600. Do not commit or share the raw state.
Request records redact v1 API key path components and omit request values,
response bodies, and authentication headers.

The token-protected `POST /probe/link` is the virtual pairing button and
opens the normal 30-second pairing window. `GET /probe/status` exposes only
the simulated state and non-secret client labels.

On Docker Desktop, multicast inside the VM is not proof of LAN discovery.
During this run, a separate host `dns-sd -R` advertisement was registered for
`_hue._tcp` on port 80 with `bridgeid` and `modelid` TXT records. The app can
also use the host IP manually. Whether discovery was automatic or manual in
the successful iPhone attempt was not independently recorded.

To stop only this probe, preserve its state, and free its published ports:

```sh
docker compose -p smart-lighting-hue-probe down
```

Stop the task's separate `dns-sd` registration too. Do not stop unrelated
containers. The existing Python/Govee controller is independent.

Reference protocols and license belong to [diyHue](https://github.com/diyhue/diyHue).
