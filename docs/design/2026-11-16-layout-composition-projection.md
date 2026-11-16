# Layout-driven composition and screen projection

## Accepted behavior

Light Studio is the source of fixture placement and composition membership.
Raising a floor lamp changes its visible segment selection and screen sampling;
removing a Play stops its composition output. Keep Hue/HomeKit identities,
pairing, saved scenes, calibration, owner power coordination and packet budgets.
Scene playback lights all segments of included fixtures. Only Sync uses the
visibility and screen-extension model.

## Capture and projection

Keep the established virtual capture resources and 18/14 channel identities.
Their stable screen-perimeter sampling basis is internal capture geometry,
independent of physical placement. The monitor strip provides nine U-shaped
samples and the three virtual Play sources provide the bottom-edge palette.
Source capture remains available when its corresponding physical fixture leaves
the composition. This avoids changing the Box's channel topology/cache for every
drag or membership edit.

`CAPTURE_POSITIONS` maps the semantic perimeter UV points exactly onto the
existing monitor-space ±0.4 coordinates; legacy `POSITIONS` remains available.

Generate a pure profile from the validated saved layout. Convert physical
positions to monitor-relative coordinates and sample the nearest perimeter edge,
interpolating between adjacent raw RGB16 samples. Apply per-fixture brightness
after projection. Do not feed already calibrated physical output back into the
capture palette.

The H6076 profile has a 136 cm body, with the existing schematic anchor 60 cm
above its base and an approximate emitting span 6–134 cm above its base. Seven
equal emitter segments intersect the region above desk height for Sync; fully
hidden segments receive black. Sample the visible portion of a straddling
segment. Raising the base above the desk exposes all seven segments. Play uses a
broad wall-wash profile: its default wall sample is at least at desk height even
when its body is below the tabletop. These are documented front-wall defaults,
not a photometric/ray-traced occlusion model. Orientation stays user controlled.

## Membership and compatibility

Extend v3 rows with optional `enabled`, default true; serialize false only.
Retain six enrolled inventory rows and their geometry/calibration while hiding
excluded fixtures. The monitor anchor remains independently editable when the
strip is excluded. Picker rejoin uses the same enrolled `layout_id`, preserving
state and preventing duplicate identities. New discovery bindings remain
configuration-only until explicit physical enrollment exists.

Gate scene writes and Govee rendering by current membership. Clear obsolete jobs
and dedup state on exclusion so rejoin cannot replay an old target. Exclusion
does not issue power commands or delete accessories.

For native Hue Sync, retaining excluded fixtures in an owned Entertainment stream
would still control them. Reconcile only the selected, inactive/owned native area
against the existing authorized four-light/twelve-member inventory. Never alter
a foreign active stream or address unknown resources. Update its service locations
for the selected subset, validate readback membership and channel/member mapping,
then stream only those channels. Empty native selection releases native streaming
while Govee Sync can continue. Keep the native area UUID; map returned channel IDs
by service/index rather than assuming fixed numeric positions. Geometry changes
resample locally; membership changes perform a fenced native handoff.

Native area updates must preserve each service's `equalization_factor` as well
as its positions. The actual inactive-bridge capability probe confirmed subset
updates and stable IDs, but also showed omitted factors reset to defaults. Store
validated full-inventory location metadata atomically in the private state
directory. Retain excluded entries so rejoin after restart restores the original
metadata. With a configured cache, missing metadata or failed persistence blocks
the membership PUT rather than inventing replacement calibration.

## Acceptance

Test pure visible masks, raising/lowering fixtures, monitor position/size, Play
below the desk, disabled roles, interpolation, raw-color calibration and stable
capture channels. Test v3 omission compatibility, remove/reload/rejoin and conflict
merges. Test no writes to excluded roles, no stale replay, native subset readback,
foreign ownership, malformed metadata and all-native-excluded behavior. Run source,
archive and isolated Linux checks and independent review before a bounded local
cutover. Preserve rollback and the owner's layout/power state; never launch games.
