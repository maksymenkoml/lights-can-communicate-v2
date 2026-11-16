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
| Virtual Bridge cold restart / Docker UDP publication | Idle guard activated; repeated real Start/Stop verified | Persistent Linux UDP bind retains delivery while inactive; localhost/LAN-self probes passed after both real Stops. Arbitrary external-proxy repair is not implemented; see evidence/restart-recovery.json |
| Play intent after cold restart | Activated and live telemetry verified | Six dynamic lights restored, IDs and already-On power preserved. Off replay, partial override, hold, cancellation and rejected requests covered in isolated tests; no new visual acceptance |
| Source starts while fixtures are Off | Offline verified; renewed live output incomplete | Same source waits for all four fresh native On/normal states, then starts without power writes or a source restart. Initial PS5 input was received while Off; the operator stopped that source before powering fixtures. See RECOVERY.md and evidence/renewed-sync-attempt.json |
| Native warmup and transient state recovery | Implemented and live exercised | No frame while native cache is stale; fresh owner off stays blocked |
| Discovery and changed IP recovery | Implemented, offline verified | Candidate identity receives read-only status probe before any route can authorize writes |
| Power ownership through existing Apple Home/Homebridge | NAS overlay installed; owner confirmed automatic Right Off; On API verified | Existing setter pauses zoned color before Off and grants after On; no local harness needed. The owner accepted current Left behavior; latest physical Right On and whole-Home scene execution are separate remaining acceptance gates. See ../homebridge-power/README.md |
| Native power confirmation and SDK color writer coordination | Installed on existing NAS plugin | Fresh identity-bound observations, bounded retries, serialized heartbeats, debounced writer drain and stale SDK callback fencing; 63 Node and 259 Python tests pass |
| Coordinator unavailable during existing Home Off/On | Live Home/NAS/native readback checked | Bounded pause preserved original Home power; resume retained both Off after Off and restored seven-zone animation after On. First recovery proof ordering was not captured live; it has offline coverage |
| Apple Home power state reflected in Hue | Agent verified actual iPhone UI | Home On/Off, Right On during Play, both Off during Play, scene recall with Right Off and Stop checked through Mirroring. Rechecked after native confirmation: both Home On/Off paths, Stop/Play and static scene recall with Right Off. Short background resume was interrupted by iPhone use and remains inconclusive. Hue-to-Home power commands are separate work |
| Left Home Off/readback reliability | Partial; bounded 4/s profile measured | FIFO removes concurrent replay failures. Uncapped Play still exceeded the confirmation window. Three later 4/s Off cases converged in 4.78–5.41 s; owner subsequently reported Left works as expected. See evidence/confirmed-power-live.json and evidence/iphone-confirmed-power-live.json |
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
| Automatic discovery and real grid placements | Activated; API and automated DOM verified | Existing sole Govee listener plus paired-Hue GET catalog; explicit canonical selection/autosave/offline edits. New bindings are configuration only. Owner tab was not opened; see ../../core/evidence/discovery-layout.json |
| Applying arbitrary topology from setup UI | Not implemented | Current physical driver and native IDs remain pinned; zoned output is a separate explicit pilot mode |
| Packaged Python adapter | Activated locally; transport/API verified | Reproducible artifact, strict version-one configuration, no-IO preflight and SIGTERM cleanup. Real scene/Sync/Stop coverage; pinned desk profile remains. See ../../core/evidence/local-cutover.json |
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
