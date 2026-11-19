# Light Studio redesign

## Why

The owner found the current Light Studio outdated and clumsy, both in look and
in use. A standalone prototype was iterated with the owner element by element
(2026-11-16 to 2026-11-18). This document fixes the agreed design and the plan
to port it into `studio/`.

Backward compatibility of `desk-layout.json` and of the setup server API is not
required (owner decision, 2026-11-17). Existing files are migrated on load.

## Agreed design

- **The page is the room.** No header band. "Light Studio | Office ✓" sits as
  plain text in the top-left corner; the check mark means the layout is saved.
  The bottom has three floating panels: Lamps (left), status + Sync switch +
  light/dark scene (centre), undo/redo (right).
- **Drafting look.** Commissioner type, cool paper `#ebf0f3`, half-tone wall
  `#c9d3db`, navy ink `#0e2235`, teal accent `#0b7680`; 5 px controls, 8 px
  panels, 12 px type floor. Navy night scene `#0f1c2a` with accent `#5cc6cd`.
  The light/dark switch is a preview of the room, not a theme of the controls.
  All UI text is English and short.
- **Objects are edited in place.** Selecting an object dims the rest to 22 %.
  Every object has the same controls in the same places: a name chip above,
  height dimension on the left, offset from the screen centre at the floor,
  resize knobs on its edges, brightness correction rail on the right of lamps.
  Dimension labels are click-to-type. Positions snap to 5 cm.
- **Light behaves like light.** The wall wash and glow of every lamp sit
  behind all objects. In Sync a floor lamp lights the wall only next to its lit
  zones. Lamp light updates live while dragging. Switching the Sync view
  cross-fades the whole room.
- **Sync view.** The Sync switch chooses which settings are shown; it does not
  start or stop Sync. With it on, the selected lamp shows a ray from each lit
  zone to its sample point on the screen; a selected monitor shows rays to every
  lit zone of every lamp and keeps those lamps undimmed. The falloff zone below
  the desk is an object with six corner knobs (table-relative, `sync_zone.py`),
  with Auto and reset.
- **Live status** is a pill left of the Sync switch, visible in both views:
  Syncing / Waiting for frames / Hue busy / Not syncing / Sync status unknown /
  "<lamp> <reason>" / "N lamps not lit". Clicking it selects the lamp with the
  problem. Blocked lamps get a "!" badge and render unlit; their chip and card
  name the reason.
- **Lamps panel** (one list): legend rows with the live light swatch (zone
  colours in Sync), name, model and zones, and a status (blocker, not connected,
  outside Sync, correction %). Hover marks the lamp in the room; click selects.
  Desk and monitor rows show their sizes. A "Found" section lists discovered
  lamps; "+" places one with a click in the room. Removing is in the row (trash
  on hover) and in the card, always with an inline Cancel / Remove confirmation;
  removed lamps return to "Found".
- **Lamp card** (from "⋯" on the chip): editable name, model and zones, Sync
  blocker with a hint, Blink and Reset correction tiles, Remove. It opens beside
  the lamp, never over it, and does not move when settings change.
- **Tooltips** appear only on icon-only controls and never repeat visible text.

## Runtime mapping

| Design element | Runtime today | This port |
|---|---|---|
| Position, size, orientation, Sync zone, correction | layout v3 fields | kept |
| Live colours and Sync status | `/api/pilot` (`sync.state`, `reason`, `blockers`, lamp colours) | read as today; unknown reasons stay "unknown" |
| Remove from layout | `enabled: false` (excluded from composition) | kept |
| During Sync: Own light | composition exclusion restores the lamp's own scene | new row field `syncRole: "own"`; excluded like `enabled: false`, but the lamp stays in the room |
| During Sync: Off | none | follow-up (adapter must switch the lamp off and restore it) |
| Connected / Connect a found lamp | none: `device-*` rows are configuration only | follow-up (explicit enrollment); found lamps show "Not connected" |
| Correction per Sync state | one `scale` | follow-up; one correction for now |

## Plan

**PR 1 (this branch): the new UI.**

- Replace `static/index.html`, `app.js` and `style.css` with the redesigned UI,
  wired to the existing layout, discovery and pilot APIs.
- Self-host Commissioner (OFL) as static assets; add them to the server asset
  list and to the managed-service build.
- Layout version 4: adds the optional row field `syncRole` (`"own"`; `"sync"`
  is the default and is not stored). Version 3 files load as version 4.
  `sync_projection` treats `syncRole: "own"` like an excluded row.
- Keep the server contracts: loopback/origin gate, validated writes, If-Match
  revisions, discovery bindings, pilot projection, sleep hold, 50/1000/1500 ms
  status cadence, hidden-page suspension, local draft and field-wise merge.
- Rewrite `tests/setup_studio_client.test.cjs` against the new UI; extend the
  layout and projection tests for `syncRole`; update the README.

**PR 2 (follow-up): adapter support** for Off during Sync, explicit connect /
disconnect of found lamps, and a correction per Sync state. It must respect the
HomeKit power authority (`/power-owner` may be absent) and the new receiver
reasons and idle state from #27.

## Non-goals

- No change to running services, NAS installs, pairing, power or light output.
- No keyboard editing beyond what the browser gives, and no phone layout.
- The page never opens itself; the owner opens it.
