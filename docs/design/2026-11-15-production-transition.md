# Production transition

Status: proposed target and migration gates, grounded in the current pilot.
No NAS activation, package publication, device migration or runtime replacement
is authorized by this document alone.

## Intended outcome

Install and operate Smart Lighting on the NAS through its own Homebridge plugin.
The Mac must not be needed for scenes, video sync, power coordination or setup.
Retain the tested six-light behavior and the existing Apple Home accessories,
automations and Hue pairing. An unrelated device must never become a target
because it was discovered or reused a saved IP address.

## Current liabilities

- The adapter, virtual Hue bridge and layout UI depend on the Mac.
- The NAS power integration modifies an exact third-party plugin version.
- Python modules live under experiments and import one another as scripts.
- Physical target profiles, virtual roles and native area/channel mapping still
  contain desk-specific assumptions. The layout UI does not provide arbitrary
  physical topology support.
- The virtual Hue integration wraps a pinned diyHue runtime; compatibility with
  another version is not established.
- No distributable own Homebridge package, supported installation path,
  transactional deployment or NAS network/lifecycle acceptance exists.

## Recommended structure

A persistent NAS core owns virtual Hue API compatibility, scene playback,
Entertainment reception/relay, device drivers, output arbitration and durable
state. Keep the tested Python implementation while extracting explicit component
interfaces. Use a pinned, isolated Hue compatibility boundary until replacement
has independent parity evidence.

An own Node/TypeScript Homebridge plugin owns the HomeKit integration, setup UI,
core connection/status and supported operator workflow. Homebridge supports
custom plugin UI; this is the intended home for the existing schematic layout.
Do not put video frame processing in the Homebridge UI server.

Keep the core and Homebridge as separate managed processes. Core scene/video
availability should not depend on an open browser or UI request lifetime. The NAS
network attachment and published Hue HTTPS/UDP discovery endpoints require a
read-only host/network inventory before choosing host networking, a dedicated
LAN address or another supported attachment. No particular NAS network mode is
assumed ready.

## Alternatives

- All runtime behavior inside a single Homebridge plugin: fewer deployment
  pieces, but couples Hue networking, protocol dependencies and streaming
  lifecycle to the Homebridge runtime. Not the recommended first transition.
- NAS core plus own Homebridge plugin: preserves proven drivers and separates
  protocol service lifecycle from the HomeKit integration. Recommended; the
  installation workflow must manage the extra service explicitly.
- A complete TypeScript rewrite: not required for the transition. Reconsider
  individual components only after explicit parity tests exist.

## Delivery slices

1. Reproducible core distribution: versioned configuration and entry points,
   explicit state location, compatibility adapter, sanitized health/status,
   orderly shutdown and a reproducible isolated image. No new physical topology
   or live replacement. Existing pilot remains a rollback path.
2. Own installable Homebridge package: custom setup UI and authenticated core
   contract; selected device/profile/placement configuration; migration rehearsal
   using copied synthetic accessory state. No duplicate accessories by default.
3. Remove the third-party overlay: own power integration and a verified migration
   for the exact existing HomeKit identity and service layout. Preserve bridge
   identity, accessory UUID, AID/IID relationships, pairing and scene bindings.
   Matching display names is insufficient. If this cannot be demonstrated,
   resolve migration design before changing the live accessories.
4. NAS rehearsal and cutover: verify CPU architecture, runtime support, network
   ports/discovery, persistent permissions, secret storage and startup ordering.
   Transfer existing virtual Hue identity and durable state through a bounded
   backup/restore workflow. Fence the previous writer before granting the new
   one. Live activation requires its own concrete deployment approval.
5. Release gate: install, restart, interrupted update, rollback, stale/unavailable
   devices, changed IP, Off/On races, loss of core/Homebridge/network and repeated
   Sync/scene transitions. Verify existing automations and both applications.

## First slice acceptance

- One documented service invocation and one versioned configuration location;
  no dependence on the caller's working directory or implicit experiment paths.
- Build contains only public runtime code and pinned dependencies, with no real
  credentials, device identities, captured state or local archives.
- Deterministic component imports and explicit runtime dependency checks.
- Startup, readiness and shutdown checks distinguish process health, upstream
  availability and output permission. Unavailable devices do not authorize output.
- Existing scene/video, discovery, power and recovery checks exercise the packaged
  path with synthetic state, preserving established behavior.
- Existing live pilot entry points and stored formats remain supported until a
  separately verified cutover. No hardware commands are needed for packaging tests.

## Release acceptance

The supported first release may target the already tested Govee H6076 and Hue
profiles. Discovery must identify capabilities without automatically enrolling
new devices. Unknown models remain unsupported until a driver/profile is tested.

Production readiness requires NAS operation with the Mac shut down; preserved
HomeKit identities/automations and virtual Hue pairing; single-writer handover;
recoverable updates/backups; understandable diagnostics; stale-browser migration across layout schema changes, and explicit physical
acceptance. Passing the existing unit suite alone does not establish these gates.

## Primary references

- Homebridge custom UI: https://github.com/homebridge/plugin-ui-utils
- Current implemented subset: experiments/hue-bridge/FEATURES.md
- Recovery evidence: experiments/hue-bridge/RECOVERY.md
- Installed overlay limits: experiments/homebridge-power/README.md
