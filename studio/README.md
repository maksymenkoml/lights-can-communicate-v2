# Light Studio (local setup studio)

Light Studio is the front wall of the desk drawn as a room: the desk, the
monitor and the selected lamps at their real sizes. Edits save automatically to
the desk layout that Sync uses. The design is described in
`docs/design/2026-11-18-light-studio-redesign.md`.

Run from the repository root:

```sh
python3 -B studio/server.py
```

Open `http://127.0.0.1:19881` yourself; the server never opens a browser. The
server binds loopback, accepts only local `Host`/`Origin` requests and serves an
explicit asset list (`/`, `/app.js`, `/style.css` and the self-hosted
Commissioner font, SIL OFL 1.1, see `static/fonts/OFL.txt`). Project and state
files and credentials are never served. The page makes no external requests.

## The room

- "Light Studio | Office ✓" in the top-left corner; the check mark means the
  layout is saved. Bottom panels: Lamps (left), Sync status, the Sync switch and
  the light/dark scene (centre), undo/redo (right).
- Select an object to edit it in place: name chip above, height on the left,
  offset from the screen centre at the floor, resize knobs on its edges and the
  brightness correction rail on the right of lamps. Click a dimension to type an
  exact value. Positions snap to 5 cm. Every edit, including undo, saves after
  300 ms; there is no Save button.
- The Sync switch chooses which settings are shown; it does not start or stop
  Sync. In the Sync view a selected lamp shows a ray from each lit zone to its
  sample point on the screen, a selected monitor shows the rays of every lit
  lamp, and the falloff zone below the desk has six corner knobs, Auto and reset.
- The lamp card (from "⋯" on the chip) shows the lamp, its Sync blocker with a
  hint, "During Sync: Sync / Own light", Reset correction and Remove. Removal
  always asks inline; removed lamps return through "Found".
- The light/dark switch previews the room at night. It and the Sync view are
  browser preferences and are not part of the layout.

## Layout API

`GET /api/layout` returns `{layout, revision}`. `POST /api/layout` takes the
whole layout with `If-Match: "<revision>"` (`""` when nothing is saved yet). A
missing header returns 428 and a stale revision returns 409; the client then
refetches and merges its changes field by field. Writes must use the current
layout version (5); older versions are refused. The browser keeps a
draft (`light-studio-grid-v3`, `-base`, `-pending`): offline edits are retried
every 3 s, and the first load imports a browser draft when the server has none.

Layout version 5 stores centimetres. World x has the desk centre at 100 cm. A
floor lamp's `height` is its bottom + 60 cm; other lamps and the monitor strip
store their centre. `scale` is the brightness correction (0-100) and
`syncZone` the optional custom falloff zone (missing or null is Auto).
`enabled: false` removes one of the six role lamps from the layout and from
Sync; removing a found or drawn lamp deletes its row. The optional
`syncRole: "own"` keeps a lamp in the room but outside the Sync composition, so
it keeps its own light; `"off"` (role lamps only) also leaves the composition and
switches the lamp off while Sync runs, restoring it afterwards (see
`core/README.md`, "Lamp controls"); `"sync"` is the default and is not stored.
Outside Sync, own-light and off lamps still get scenes. The six role rows accept
an optional `name` (1-35 characters after stripping, 1-32 for the four Hue lamps,
no control characters), stored only when given: it is the lamp's name in Light
Studio, on the virtual Hue bridge, in HomeKit and on the physical Hue light
(see `core/README.md`, "Lamp names"). A row without `name` renames nothing; an
invalid name refuses the whole write. Version 5 adds
the second, without-Sync (scene) configuration: row `sceneScale` (0-100; a row
without it gets a copy of `scale` once, after which the two change
independently), top-level `sceneZone` (same format as `syncZone`, missing or null is
Auto) and the booleans `syncFalloff` (default true) and `sceneFalloff` (default
false). The defaults are not stored. A fade that is off gives every gain 1 and
samples Govee segments at their centres. Scene output includes every present
lamp, own-light ones too. A write that omits `sceneZone` keeps the saved one,
like `syncZone`; omitted fade switches mean their defaults. Version 3 and 4
files load as version 5, so their revision changes and pages opened before the
upgrade must refetch. Writes accept geometry and corrections only: no power,
physical identifiers or commands.

## Blink

`POST /api/identify` with `{"lamp": "<role lamp id>"}` (for example
`"govee-left"`) asks the adapter to blink that lamp so the owner can find it. It
has the same local-origin protections as layout writes (Host, Origin,
`Sec-Fetch-Site`, `application/json`). Only the six role lamps can blink: other
ids return 404, a malformed body 400. The answer is 200 `{"accepted": true}`,
409 `{"accepted": false, "reason"}` when the adapter refuses (`lamp off`,
`writer closed`, `sync active`, `busy`, `unavailable`, `brightness unknown`, or
`paused` during the owner's live-output hold) and 503 when the adapter is not
reachable or the blink was not delivered. Only an owner click triggers it.

## Found lamps

The Lamps panel polls `/api/discovery` every 3 s only while it is open. Found
lamps that are supported, online and unambiguous can be placed with "+" and a
click in the room. They become `device-*` rows with their canonical name, model,
shape and zones, which the server verifies before saving. These rows are
configuration only: they show "Not connected", stay neutral in the room and do
not enroll any physical output. Unavailable lamps show their reason (Offline,
Unsupported, Ambiguous).

## Live colours and Sync status

The page polls `/api/live` 50 ms after each answer (about 20 updates a second,
close to the Sync frame rate), with a 1000 ms request
deadline and an independent 1500 ms freshness guard; hidden pages stop polling.
Each enrolled lamp is painted from its emitted zone colours (Govee seven zones
bottom to top, the monitor strip nine, Play one). A lamp is drawn unlit when it
is off, stale, offline, excluded or blocked. In the Sync view the monitor shows a
soft gradient of the strip's colours while Sync is active. Telemetry only
repaints existing SVG attributes: it never rebuilds the layout and never saves.

The status pill shows Syncing, Waiting for frames, Hue busy, Not syncing, Sync
status unknown, "<lamp> <reason>" or "N lamps not lit"; unknown reasons fall back
to these labels. Clicking it selects the lamp with the problem. A local sleep
hold (`live-output-hold`) reports Not syncing without contacting the adapter.

## Hardware notes

The H6076 floor lamps have seven physically verified zones behind three Signe
sampling anchors; native monitor streaming has nine verified segments; each Play
has one colour channel. Positions are relative to the monitor and published
through the virtual Entertainment API. The 32-inch monitor uses an assumed 16:9
aspect ratio; Govee is drawn 136 cm tall and Play 25 cm long.
