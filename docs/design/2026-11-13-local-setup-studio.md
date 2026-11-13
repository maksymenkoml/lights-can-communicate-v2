# Local setup studio spike

The owner requested a local sketch for lamp types and placement, then specified
one simple schematic with automatic decisions wherever possible. Placement
must snap to a fixed grid, nominally 10 cm, rather than arbitrary pixels.

## Interaction

One front-view diagram contains the monitor and six selected lamps. Known
models supply form, zone template and mapping. There is no
separate device list, navigation rail, zone editor or persistent inspector.
The owner drags lamps or uses arrows; every move snaps to a 10 cm cell.
Selecting a lamp reveals its model, position and brightness. Adding a virtual
lamp asks only for its form. Save persists the local draft.

Existing scale defaults remain 65% for Govee, 50% for native Center, and 100%
for other native lamps. The hidden right-driver packet budget remains 10/s.
Version 3 stores centimeter coordinates in browser local storage and migrates
earlier position/brightness drafts. Glyphs are illustrative, not dimensioned
CAD objects. The strip and monitor move as one attachment. Form-specific
bounds and grid alignment reduce manual decisions.

## Boundary

Python serves a dependency-free frontend on loopback only. Assets are explicitly
allowlisted. An optional sanitized read-only status endpoint keeps its adapter
token on the host; the minimal frontend does not poll it. No lighting, power,
native-area or pilot-configuration write endpoint is present.

The monitor has nine verified Entertainment segments and five scene palette
points; each Play has one channel. Current Govee output remains one whole-lamp
color. Its segmented preview is an automatic model template, not a claim of
validated zoned LAN output. Physical segmentation is separate work.

This local UI is a feedback spike before Homebridge packaging. The existing
lighting process, allowlist, scenes and calibration remain independent.

The desk is 160 cm wide with its top 85 cm above the floor. The 32-inch
monitor uses an assumed 16:9 aspect ratio (about 71 × 40 cm), drawn at the
same scale as the grid. Lamp glyphs remain schematic. Saved centimeter
positions use the floor as their height origin.

Selecting the desk opens just width and tabletop height. Defaults are
160 and 85 cm; both persist with the layout. Changing height carries the
monitor with the tabletop. The view expands to fit wider desks.

Selecting the monitor edits its diagonal in inches (default 32, 16:9).
The display and attached strip resize together and the diagonal persists.

Play orientation is an explicit horizontal/vertical selection, persisted per
lamp; moving it never rotates it. Lamps can move beyond the desk edges.
The view expands after release and stays stable during pointer dragging.

Owner-specified fixture dimensions are used at the same scale: Govee
height 136 cm and Play length 25 cm, including when Play is vertical.
