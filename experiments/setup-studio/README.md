# Local setup studio spike

One schematic of the selected desk lights. Drag lamps onto a fixed 5 cm grid,
select one to adjust brightness, with automatic saving. Known models supply form,
segment template and color mapping. The six existing desk roles provide layout
and brightness corrections to the opt-in live pilot; extra drawings stay unbound.

Run from the repository root:

```sh
python3 -B experiments/setup-studio/server.py
```

Open `http://127.0.0.1:19881`. The server binds loopback and serves only three
public assets, the validated layout API and an optional sanitized read-only
pilot status projection. Project/state files and credentials are never served.
Layout writes accept geometry and correction percentages only, without power,
physical identifiers or arbitrary commands. The frontend does not poll the pilot.

Version 3 stores centimeter coordinates and a fixed 5 cm step. The first load
imports an existing browser draft when the server has no saved layout. The
private server copy then becomes authoritative; local storage retains offline
edits for retry. Concurrent edits merge by changed field. Earlier drafts retain
position and brightness on migration.
Keyboard arrows move one grid cell; form-specific bounds retain glyphs on the
stage. The diagram is schematic, not a dimensioned CAD drawing. A new virtual
lamp asks only for its form.

The H6076 pilot has seven physically verified zones behind three Signe sampling
anchors. Native monitor streaming has nine verified segments; each Play has one
color channel. Positions are relative to the monitor and published through the
virtual Entertainment API and SSE. The actual Sync Box consumed live position
changes in a controlled co-location/restore check: all 25 co-located seven-zone
targets matched, then diverged again after restoring the owner's positions.
This measures target selection, not optical LED timing. Scene
palettes keep Hue's existing behavior; placement affects Entertainment sampling.

The desk is 160 cm wide with its top 85 cm above the floor. The 32-inch
monitor uses an assumed 16:9 aspect ratio (about 71 × 40 cm), drawn at the
same scale as the grid. Lamp glyphs remain schematic. Saved centimeter
positions use the floor as their height origin.

Selecting the desk opens just width and tabletop height. Defaults are
160 and 85 cm; both persist with the layout. Changing height carries the
monitor with the tabletop. The view expands to fit wider desks.
The table stays fixed on the floor. Moving or rotating a physical fixture and
resizing/moving the monitor clamps its full body above the floor; older saved
below-floor anchors are raised on load. Labels can extend into the shaded strip
below the floor, but physical objects cannot.

Reference guides span the complete scene, including the monitor center, table
center and tabletop. The optional Sync bounds use a closed solid perimeter and
smoothstep-weighted shading. All six corners can move on the 5 cm grid. Edits
autosave, support undo and affect actual Sync output. Custom points stay attached
to table width and tabletop height; Auto restores the placement-derived 15 cm
band and lateral spread. Functional bounds may extend below the floor.

Selecting the monitor edits its diagonal in inches (default 32, 16:9).
The display and attached strip resize together and the diagonal persists.

Play orientation is an explicit horizontal/vertical selection, persisted per
lamp; moving it never rotates it. Lamps can move beyond the desk edges.
The view expands after release and stays stable during pointer dragging.

Owner-specified fixture dimensions are used at the same scale: Govee
height 136 cm and Play length 25 cm, including when Play is vertical.

The monitor itself supports pointer dragging and arrow movement on the
5 cm grid, plus numeric center height from the floor and offset from the
desk center. The attached strip shares its position. Elevated placements
persist without being pulled back into the initial viewport.

Every edit (including undo) saves automatically to local storage and the server. There is
no Save button, reset action or persistent save/instruction footer.

The desk uses solid, thicker blue-gray tabletop and legs. Width is printed
on the tabletop and height directly along the right leg, without separate
dimension lines. Moving to a 5 cm grid preserves saved centimeter positions.


## Discovered-device picker

The add menu reads `/api/discovery`, a local-origin-protected proxy for the
adapter's authenticated catalog. It never exposes adapter credentials. Select
supported available lights to add identity-bound `device-*` placements; existing
six-role placements and older drawings are retained. New placements autosave,
round-trip and can be edited/removed offline. Their canonical type/model/name/
zones are verified before initial persistence. They are setup configuration and
do not enroll additional physical outputs in this component.

The picker polls only while open, preserves checks/focus across refreshes, and
shows unavailable or unsupported choices. Refresh an existing browser tab after
the update. Govee must advertise through LAN Control; Hue comes from the already
paired Bridge inventory. The local API/DOM checks passed; visual browser
acceptance remains with the owner because he requested no automatic layout open.


## Prepared zone colors and Sync status

The next gateway/UI version consumes the adapter's emitted-output preview through
`/api/pilot`; only bounded known-role RGB/power metadata reaches the browser.
Hue scene stops are sampled across the monitor's nine display zones. Calibration
is already applied and the browser does not scale colors a second time.

Sync states distinguish active, waiting, off, external streaming, unknown and
explicit pause. Source activity is read from the virtual bridge only when the
adapter snapshot cannot decide the state, using a bounded shared cache. A local
sleep hold returns paused before any adapter/source fetch. No physical Hue/Govee
reads or control requests are performed by the UI gateway.

The client refreshes every 200 ms with a 1000 ms request deadline and an independent
1500 ms freshness guard; hidden pages suspend requests. Color updates retain SVG
nodes, dragging, pointer capture, keyboard focus, autosave and discovery state.
Source-unknown status can coexist with fresh known command colors. Off does not
require color history. New configuration-only placements remain neutral.

Prepared during the owner's sleep hold. The running gateway/physical runtime has
not been replaced for this version; activation and visual acceptance require the
owner's explicit morning resume. Do not open the owner's layout tab automatically.
