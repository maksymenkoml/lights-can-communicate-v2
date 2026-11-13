# Pilot features and Homebridge migration scope

Status separates owner/physical acceptance, API observations, offline checks,
and work that has not been implemented. Current physical video acceptance covers the PS5 menu and an owner-launched
game. The owner reported that everything works great. The game title and
optical timing were not recorded.

| Capability | Pilot status | Evidence or limit |
| --- | --- | --- |
| Official Hue iPhone pairing and test room | Owner accepted | Separate virtual Bridge identity and saved pairing |
| Static shared palette on six lights | Owner accepted | Two Govee, one monitor strip, three under-desk Play |
| Gallery Vapor wave, Play and Stop | Owner accepted | Smooth shared animation; other gallery scenes not individually accepted |
| Govee brightness calibration | Owner accepted | Both at 0.65 of scene brightness |
| Native Hue calibration | Owner accepted | Center Play 0.50; strip/other Play 1.00 |
| Native-only Sync priority and scene return | Owner accepted | Original path pauses our writes during foreign streaming |
| Actual Sync Box 8K video relay | Owner accepted in PS5 menu and game | Real v2 RGB input; two Govee LAN outputs plus native Hue DTLS relay |
| Video Stop returns to Vapor wave | Owner accepted | Native normal-mode/scene command readback also recorded |
| Right controller pacing | Implemented and menu accepted | Measured packet pressure; separate 10-command/s cap counts dimming and color together |
| Fresh-frame recovery after transport gap | Implemented and live observed | Fresh higher sequence resumes; explicit Stop retires session |
| Native warmup and transient state recovery | Implemented and live exercised | No frame while native cache is stale; fresh owner off stays blocked |
| Discovery and changed IP recovery | Implemented, offline verified | Candidate identity receives read-only status probe before any route can authorize writes |
| Power ownership through existing Apple Home/Homebridge | Preserved | No pilot power writes; prior owner-operated left off/on accepted with scene path |
| Older right LAN/Wi-Fi recovery | Physically exercised | Saved Wi-Fi reapplied; full power/controller restart restored scan/status; no factory reset |
| Third Govee and unrelated native Hue | Excluded | Fixed allowlist; discovery alone never enables output |
| PS5 gameplay | Owner accepted | Both Govee with Hue; game title not recorded |
| Xbox, HDR and refresh-rate combinations | Not separately tested | Do not infer complete coverage from one game |
| Optical latency and fully black frames | Not measured/accepted separately | Packet timing and black-without-power behavior have offline coverage |
| Owner off/on during our native video relay | Offline verified only | Fresh native off releases relay and requires source restart |
| Local setup UI sketch | Implemented as a draft | Simple schematic, automatic model defaults and fixed 10 cm placement; no physical apply |
| Applying arbitrary topology and zoned Govee output | Not implemented | Current physical driver and native IDs remain pinned |
| NAS service and own Homebridge package | Not implemented | Runtime is still on the Mac with a pinned reference bridge |

## What the Homebridge version must retain

- One source of scene/video priority, calibrated per-lamp output and measured
  per-device packet budgets.
- Stable virtual resource identity, room/scene persistence and pairing state.
- Exact device selection after discovery; no automatic control of newly found
  devices. Native Bridge discovery must not import unrelated lights or areas.
- Existing Apple Home power scenes and accessory identity. Adding duplicate
  HomeKit lights is not the migration strategy.
- Separate host-side native credentials, bounded queue/replay checks, cleanup
  of only our stream, and recovery after unavailable networks or restart.
- Operator configuration for roles/positions, scale, packet budget, native
  Bridge/area mapping and diagnostics, replacing prototype constants.
- NAS networking and service lifecycle validation, including Hue discovery,
  HTTPS identity and UDP Entertainment reachability.

Package publication, NAS deployment and a replacement setup UI are separate
work. This pilot does not authorize a release or establish complete Hue API
compatibility.
