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
