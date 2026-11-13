# Local setup studio spike

One schematic of the selected desk lights. Drag lamps onto a fixed 10 cm grid,
select one to adjust brightness, with automatic local saving. Known models supply form,
segment template and color mapping. No physical lighting settings
are changed.

Run from the repository root:

```sh
python3 -B experiments/setup-studio/server.py
```

Open `http://127.0.0.1:19881`. The server binds loopback and serves only three
public assets plus an optional sanitized read-only pilot status projection.
Project/state files and credentials are never served. POST requests cannot
control lights. The minimal frontend does not poll the running pilot.

Version 3 stores centimeter coordinates and a fixed 10 cm step in browser
local storage. Earlier drafts retain position and brightness on migration.
Keyboard arrows move one grid cell; form-specific bounds retain glyphs on the
stage. The diagram is schematic, not a dimensioned CAD drawing. A new virtual
lamp asks only for its form.

Govee segment templates remain preview-only and require separate firmware
validation before physical zoned output. Native monitor streaming has nine
verified segments; each Play has one color channel. The running pilot remains
independent of this sketch.

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
10 cm grid, plus numeric center height from the floor and offset from the
desk center. The attached strip shares its position. Elevated placements
persist without being pulled back into the initial viewport.

Every edit (including undo) saves automatically to local storage. There is
no Save button, reset action or persistent save/instruction footer.
