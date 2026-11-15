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
| Govee LAN zoned color | Owner accepted on both lamps | Correct bottom/middle/top pattern; batched reverse also accepted on left |
| Virtual Signe Gradient profiles | Owner accepted on left; API verified on both | Existing IDs retained; three scene points and three Entertainment anchors per Govee |
| Zoned Vapor wave Play | Owner accepted separately on both lamps | Multicolor gradient and smooth animation; current runtime uses explicit gradient mode |
| Eighteen-channel Sync with zoned Govee | Owner accepted on current PS5 content | Original native channel IDs retained; six logical lights; right uses a measured zoned packet limit |
| Zoned Sync Stop and scene return | Owner accepted | Both Govee return to smooth gradient Vapor wave with native strip/Play |
| Right zoned packet pacing | Measured and owner accepted | Separate 2-datagram/s gradient budget; legacy whole-color budget remains 10/s; no vendor-wide rate claim |
| Native Hue calibration | Owner accepted | Center Play 0.50; strip/other Play 1.00 |
| Native-only Sync priority and scene return | Owner accepted | Original path pauses our writes during foreign streaming |
| Actual Sync Box 8K video relay | Owner accepted in PS5 menu and game | Real v2 RGB input; two Govee LAN outputs plus native Hue DTLS relay |
| Video Stop returns to Vapor wave | Owner accepted | Native normal-mode/scene command readback also recorded |
| Right controller pacing | Implemented and menu accepted | Measured packet pressure; separate 10-command/s cap counts dimming and color together |
| Fresh-frame recovery after transport gap | Implemented and live observed | Fresh higher sequence resumes; explicit Stop retires session |
| Established DTLS pause and local fault recovery | Real Box transport/output verified | Four-second receiver pause expired video lease; fresh frames resumed output on the same handshake. Sixteen isolated lifecycle cases also passed in both channel modes; see RECOVERY.md |
| Host adapter restart during real Sync | Live observed | Existing source kept receiving; restarted host automatically reacquired ready native output |
| Virtual Bridge cold restart / Docker UDP publication | Failure localized; publication restored | Coordinated capture saw Box packets on Mac but none in container; local probes failed too. Recreating only the bridge restored UDP delivery; automatic external-proxy repair is not implemented |
| Native warmup and transient state recovery | Implemented and live exercised | No frame while native cache is stale; fresh owner off stays blocked |
| Discovery and changed IP recovery | Implemented, offline verified | Candidate identity receives read-only status probe before any route can authorize writes |
| Power ownership through existing Apple Home/Homebridge | NAS overlay installed; owner confirmed automatic Right Off; On API verified | Existing setter pauses zoned color before Off and grants after On; no local harness needed. Visual On acceptance and existing scene execution remain pending; see ../homebridge-power/README.md |
| Apple Home power state reflected in Hue | Agent verified actual iPhone UI | Home On/Off, Right On during Play, both Off during Play, scene recall with Right Off and Stop checked through Mirroring. Reliable authenticated SSE journal fixes lost updates. Hue-to-Home power commands are separate work |
| Left physical Home Off reliability | Partial | One write left fresh native On and later Home On despite closed color gate; repeat Off passed eight fresh native readbacks. Cause unresolved; UI acceptance does not prove every physical write succeeds |
| Hard adapter crash during real Sync | Live observed | SIGKILL left our native stream; new adapter reclaimed it and resumed the same Box session; old OpenSSL child disappeared |
| Coordinated right Govee Home Off/On | Local harness physically accepted; NAS automatic path API verified | Local harness: eight Off readbacks and owner-confirmed On. NAS: six fresh Off samples, then six On samples with seven zones and advancing video; visual confirmation pending |
| Older right LAN/Wi-Fi recovery | Physically exercised | Saved Wi-Fi reapplied; full power/controller restart restored scan/status; no factory reset |
| Third Govee and unrelated native Hue | Excluded | Fixed allowlist; discovery alone never enables output |
| PS5 gameplay | Owner accepted | Both Govee with Hue; game title not recorded |
| Xbox, HDR and refresh-rate combinations | Not separately tested | Do not infer complete coverage from one game |
| Optical latency and fully black frames | Not measured/accepted separately | Packet timing and black-without-power behavior have offline coverage |
| Owner off/on during our native video relay | Offline verified only | Fresh native off releases relay and requires source restart |
| Local setup UI and shared autosave | Implemented; owner confirmed layout migration | Simple schematic, 5 cm placement, atomic private server storage, offline retry and field-level concurrent edit merge |
| Live layout positions in zoned Sync | Measured with actual Sync Box | Temporarily co-located Govee produced equal seven-zone targets in 25/25 samples; restored positions produced different targets again; native channel mapping unchanged |
| Per-role brightness corrections from setup UI | Implemented, offline verified | Scene and video paths consume the same six-role layout; existing owner-accepted 0.65/0.50 defaults preserved; arbitrary slider values not separately visually accepted |
| Applying arbitrary topology from setup UI | Not implemented | Current physical driver and native IDs remain pinned; zoned output is a separate explicit pilot mode |
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

Package publication, full NAS runtime migration and a replacement setup UI are separate
work. This pilot does not authorize a release or establish complete Hue API
compatibility.
