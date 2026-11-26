# Furniture of a room

Follows the rooms design (`2026-11-24-rooms.md`, issue #78) and the table lamp
(PR #91).

## Intent

Every room layout has one desk and one monitor today. The core refuses a layout
without them, and Light Studio draws them in every room, so a new room (the
owner's Bedroom) shows a desk and a monitor it does not have. Real rooms differ:

- a room with lamps only;
- a room with a TV on a TV stand and lamps around it;
- an office with a desk and a monitor;
- a bedroom with nightstands and a table lamp.

The owner adds, moves, resizes and removes furniture per room, in the same panel
where the lamps are now. That panel is renamed. Furniture matters to light
because lamps stand on it, the screen hides light below its support, and Sync
takes colors from the screen.

## Goals

- A room holds any number of desks and cabinets (a nightstand or a TV stand),
  each with its own width and height, and at most one screen: a monitor or a TV.
- A new room starts empty: lamps only. Existing rooms keep their desk and
  monitor.
- The owner adds a piece from the panel, places it in the room like a found
  lamp, moves and resizes it with the handles a desk has today, and removes it
  with an inline confirmation.
- A table lamp or a Play released over a piece of furniture stands on its top.
- Sync works with a TV on a cabinet exactly as with a monitor on a desk.
- The Sync Box can be registered only in a room with a screen.

## Non-goals

- Other furniture (beds, shelves, sofas), furniture depth, or a top-down plan.
- More than one screen in a room.
- Changes to lights, rooms, the virtual bridge, Apple Home or the Hue app.

## Anti-goals

- A room that loses the owner's lamps, corrections, calibration, Sync zones or
  Sync behavior when its layout is migrated.
- A Sync Box room without a screen: Sync would have nothing to sample.
- Furniture that changes which lights exist or how they are driven.

## Constraints

- Layout x is in centimeters with the room's axis at x 100 (today the desk
  center). It stays the reference for lamps, the screen and every piece.
- The Sync zone and the scene zone are stored relative to the desk: `u` is a
  fraction of the desk half width, `h` is centimeters from the desk top. The
  automatic zone fades light below the desk top
  (`bridge/sync_zone.py`).
- Sync projects every lamp onto the screen edges (`bridge/sync_projection.py`);
  the Monitor Strip wraps the screen (`light_geometry`, Light Studio `wrapOf`).
- The Sync Box lies on the desktop next to the monitor (`syncBox.x`).
- Layout writes need `If-Match`; undo and redo cover every scene change.
- Light Studio strings are English. Every visual choice is shown to the owner as
  two to four variants before it is built.

## Approaches

1. **A furniture list and an optional screen (chosen).** Layout version 8 has
   `furniture`, a list of pieces, and `screen`, which may be absent. One code
   path for every piece; desks and cabinets differ only in kind and drawing.
2. **Keep `desk` and `screen`, add flags and a `cabinets` list.** Smaller
   migration, but two code paths for pieces that behave the same, and a "desk"
   that is not there would still sit in the file.
3. **Furniture as device rows.** Mixes lights with furniture in the list that the
   rooms index, the registry and the Sync composition read; every reader would
   have to skip furniture.

The owner approved approach 1 as part of this design.

## Design

### Layout version 8

```json
{
  "version": 8,
  "furniture": [
    {"id": "piece-1a2b3c4d", "kind": "desk", "x": 100, "width": 160, "height": 85},
    {"id": "piece-5e6f7a8b", "kind": "cabinet", "x": 230, "width": 45, "height": 55}
  ],
  "screen": {"kind": "monitor", "positionCm": {"x": 100, "height": 110}, "inches": 32,
             "on": "piece-1a2b3c4d"},
  "devices": [...]
}
```

- A piece stands on the floor. `x` is its center, `width` and `height` its size
  in centimeters on the five-cm grid. Ranges: desk width 80–240 and height
  50–120 (as today); cabinet width 20–300 and height 20–150. Ids are
  `piece-` plus eight hex digits, unique in the layout. At most 12 pieces.
- `screen` is optional. `kind` is `monitor` (19–55″) or `tv` (32–100″).
  `positionCm` is the screen center, as today.
- **The support of the screen** is stored in `screen.on`: a piece id, or null
  for a screen that hangs on the wall. Light Studio sets it whenever the screen
  is placed or released: the highest piece whose top is at or below the screen
  bottom and whose span contains the screen center x. Moving or resizing a
  piece does not change it. The validator refuses an `on` that names no piece.
- Versions 3–7 load as version 8: the desk becomes the first desk piece at x 100
  with its size, the screen becomes a `monitor` with `on` set to that desk,
  whatever its height. Saved zones keep their values, since that desk is their
  reference. The core writes only version 8; old
  clients are not supported (the core serves its own Light Studio).
- A new room (`rooms.new_layout`) has no furniture and no screen.

### Sync, zones and the Sync Box

- Zones are relative to the screen's support: `u` is a fraction of its half
  width around its center, `h` is centimeters from its top. For migrated rooms
  this is the old desk, so nothing moves. A zone follows its support, as it
  follows the desk today when the desk is resized: when the screen moves to
  another support, the saved zone is read against the new one.
- Without a support (a hanging screen, or no screen) a room has no automatic
  zone: the fade is off until the owner draws a zone, which is then relative to
  the floor at the room axis with a 100 cm half width. *(Builder's call, see
  below.)*
- Sync projection is unchanged: it needs the screen. A room without a screen
  has no Sync profile.
- The Sync Box lies on the screen's support (on the floor under a hanging
  screen). `syncBox.x` keeps its meaning.
- Registering the Sync Box in a room without a screen is refused
  (`RoomRefused("no_screen")`). Light Studio shows "Add a monitor or TV to this
  room first" and does not offer "Move here". Removing the screen of the Sync
  Box room is refused the same way ("The Sync Box needs a screen").
- The Monitor Strip wraps the screen when there is one. Without a screen every
  strip, the `hue-monitor` row included, is an ordinary row with its own
  `positionCm` and draws as a bar; its zones spread like any point light's
  channels (`light_geometry`). Removing a screen never removes or disconnects a
  light.
- Without a screen the Sync projection computes only zone visibility (the scene
  fade); screen sampling needs a screen.
- **Removing a piece** is always allowed. A screen standing on it gets
  `on: null` and keeps its place (it now hangs); the Sync Box then lies on the
  floor under the screen; lamps keep their heights.

### Lamps on furniture

When a table lamp (shape `table`, PR #91) or a horizontal bar (a Play, or any
strip that draws as a bar) is released or placed with its anchor
within 10 cm of a piece top, inside the piece's span, its height snaps to that
top. Floor lamps stand on the floor. Nothing is stored about the relationship;
moving or resizing a piece later does not move lamps. *(Builder's call.)*

### Light Studio

- The **Lamps** button and panel become **Objects**:

  ```
  Objects                    ×
  Lamps            3
    Govee H6022 · …   80%
  Furniture        2
    Nightstand  45×55 cm   🗑
    TV 55″                 🗑
  Add furniture
    + Desk  + Nightstand  + Monitor  + TV
  Found  …
  Sync Box …
  ```

  "Nightstand" is the English label of a cabinet. The button keeps the count
  of lamps.
- **Adding:** a "+" starts placement like a found lamp: a ghost follows the
  pointer on the five-cm grid, and a click places it. A desk or cabinet stands on
  the floor at the clicked x. A screen is placed at the clicked point. With a
  screen already in the room, "+ Monitor" and "+ TV" are disabled ("One screen
  per room").
- **Editing:** a selected desk or cabinet has the desk's handles today (width
  and height knobs, dimension labels you can type into, x drag). A TV has the
  monitor's (diagonal knob, height above its support).
- **Removing:** the trash on the row or the Delete key, always with the inline
  Cancel/Remove confirmation.
- **Drawings:** desk and monitor stay. New drawings for the cabinet and the TV
  (a thin bezel and a slim stand or feet) in the "drafting" style; the owner
  picks among variants first.
- The stage viewport fits every piece, lamp and the screen, as it fits lamps
  today.

## Stages

Each stage is one PR, verified live on the NAS before it is offered.

1. **Core:** layout version 8, migration from 3–7, empty new rooms, zones and
   the Sync Box relative to the screen's support, the `no_screen` refusal.
   Light Studio reads and writes version 8 with the same look (the migrated desk
   and monitor).
2. **Light Studio furniture:** variants of the cabinet and TV drawings first,
   then the Objects panel, adding, editing and removing pieces, snapping lamps
   onto tops, and the Sync Box and screen rules in the panel.

## Verification

- Offline: migration of every older version (pieces, screen kind, zones
  unchanged in world coordinates); support derivation (stacked and side-by-side
  pieces, hanging screen); zones and box on a cabinet; `no_screen` on register
  and on screen removal; Sync composition of a migrated layout equals the
  version 7 result; client tests for adding, editing, removing, snapping and the
  one-screen rule.
- Live on the NAS: the owner's office and Anna's office unchanged after
  migration, PS5 Sync unchanged; Bedroom without a desk and monitor, with a
  nightstand and the table lamp standing on it; a test room with a TV on a
  cabinet takes the Sync Box and Sync follows it, then the box goes back.

## Left to the builder

- The exact snap distance and the cabinet and TV size ranges above.
- The zone reference of a room without a screen support (proposed above).
- How dimension labels name a cabinet's size.
- Ids for migrated desks (`piece-` plus a stable hash of the room id is fine).
