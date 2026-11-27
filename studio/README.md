# Light Studio (local setup studio)

Light Studio is the front wall of the desk drawn as a room: the desk, the
monitor and the selected lamps at their real sizes. Edits save automatically to
the desk layout that Sync uses. The design is described in
`docs/design/2026-11-18-light-studio-redesign.md`.

Light Studio runs as the `studio` container of the NAS Compose project (see
`core/README.md`). It is published on `setup.port` (default 19881) of the NAS:
open `http://<NAS address>:19881` yourself; the server never opens a browser.
For development only, `python3 -B studio/server.py` from the repository root
serves the same pages on loopback.

The server accepts only `Host`/`Origin` values that are loopback or listed in
`container.studio_hosts` of `core.json`, and serves an
explicit asset list (`/`, `/app.js`, `/style.css` and the self-hosted
Commissioner font, SIL OFL 1.1, see `static/fonts/OFL.txt`). Project and state
files and credentials are never served. The page makes no external requests.

## The room

- "Light Studio ✓" in the top-left corner; the check mark means the layout is
  saved. The room button ("Office ⌄") is in the top-right corner and opens the
  room cards (see Rooms). Bottom panels: Objects (left), the Sync bar (centre;
  see Sync Box), undo/redo (right). The room is always drawn in the light scene.
- Select an object to edit it in place: name chip above, height on the left,
  offset from the screen centre at the floor and resize knobs on its edges.
  Click a dimension to type an exact value. Positions snap to 5 cm. Every edit, including undo, saves after
  300 ms; there is no Save button.
- The page shows and edits the settings of the current Sync state: the Sync view
  while the Sync Box syncs, the view without Sync otherwise. Start Sync and Stop
  Sync switch the view at once (a refused Start switches back). In the Sync view a selected lamp shows a ray from each lit zone to its
  sample point on the screen, a selected monitor shows the rays of every lit
  lamp, and the falloff zone below the desk has six corner knobs, Auto and reset.
- A lamp's chip is one button ("Govee Left ⌄"); it, or a double click on the
  lamp, opens the lamp card beside the lamp, on the side of the nearer screen
  edge. The card shows the lamp, its Sync blocker with a hint, the Power switch, "During Sync:
  Sync / Own light / Off", Blink, "Calibrate ›" with the brightness correction
  of the current view, and Remove. Removal always asks inline; removed lamps
  return through "Found".
- The view is not stored: neither in the layout nor in the browser.

## Rooms

- The room button opens the room cards. Each card draws its room
  small (desk, screen and a dot per lamp) with its name and lamp count; the room
  with the Sync Box shows the box, and so does the room button while there is more
  than one room. Click a card to edit that room. Hover a card for Rename and
  Delete (Delete asks inline); drag a card to change the order; the last card
  adds a room. A refusal, such as "Move its lamps to another room first", is said
  on its card.
- The page edits one room at a time. Each room has its own layout, browser
  draft and undo history, and this browser remembers the room it edits
  (`light-studio-room-v1`).
- The Sync view and the Sync bar appear only in the room with the Sync Box. In
  another room the page shows the view without Sync and the lamp card has no
  "During Sync" choice.
- Found lists the discovered lamps that no other room holds. Placing a lamp that
  belongs to the first room in another room moves it there first; Connect
  connects a lamp in the room it is in.
- "Move to room ›" on a lamp card moves a lamp to another room with its
  corrections. The page saves its edits first and reads the room again
  afterwards, so undo does not bring the lamp back.

## Layout API

`GET /api/layout?room=<id>` returns `{layout, revision}` of a room (the first
room without `room`). `POST /api/layout` takes the
whole layout with `If-Match: "<revision>"` (`""` when nothing is saved yet). A
missing header returns 428 and a stale revision returns 409; the client then
refetches and merges its changes field by field. Writes must use the current
layout version (5); older versions are refused. The browser keeps a
draft per room (`light-studio-grid-v3` for the first room and
`light-studio-grid-v3:<room>` for the others, each with `-base` and `-pending`): offline edits are retried
every 3 s, and the first load imports a browser draft when the server has none.

Layout version 5 stores centimetres. World x has the desk centre at 100 cm. A
floor lamp's `height` is its bottom + 60 cm; other lamps and the monitor strip
store their centre. `scale` is the brightness correction (0-100) and
`syncZone` the optional custom falloff zone (missing or null is Auto).
Every row is optional: a room holds any set of up to twelve lamps, or none. The
six migrated lamps are drawn from a fixed table (they store no shape or model),
and one without a row appears in Found under its registry id, ready to place. The
screen position is its own object; the monitor strip row follows it, and without
that row the monitor alone moves the screen. A layout before version 7 needs its
monitor row, which carried the screen. A new room starts empty.
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
like `syncZone`; omitted fade switches mean their defaults. Version 6 adds an
optional `calibration` on the two Govee rows: `{"warmth": -100..100, "tint":
-100..100, "lowLight": 0..100}` (integers; missing controls are 0, an all-zero
calibration is not stored). Versions 3-5 load as version 6, so their revision
changes and pages opened before the upgrade must refetch. Version 8 (current)
stores the desk as a piece in `furniture` and the display standing on it or
hanging (see `core/README.md`, "Room furniture"); older files load as version 8,
and writes must use it. Writes accept geometry and corrections only: no power,
physical identifiers or commands (power has its own route, see "Power").

## Calibrate

"Calibrate ›" slides the lamp card to its Calibrate page. Brightness is the
correction (0-100 %) of the configuration the page shows ("with Sync" while Sync
runs, else "without Sync"); a Govee lamp also has Warmth, Tint and Low light. Reset sets
colour to zero and brightness to 100 % (unsaved); Save stores the values in the
layout; back, Cancel and Esc discard them.

- **With the test** (a connected lamp while Sync is not running): the page adds
  "Compare with" (other connected lamps, default the nearest lit Hue Play, off lamps
  disabled) and Test light Bright/Dim. `POST /api/calibration` with `{"lamp",
  "reference", "level", "view", "correction"}` plus `"calibration"` for a Govee
  lamp asks the adapter to show the test white on the lamp (candidate values)
  and on the reference (its saved correction) and to darken the others; a
  change is sent at once and the request is renewed every 2 s. Leaving the page
  or closing Light Studio sends `POST /api/calibration/end`; the adapter also
  ends a session that is not renewed within 5 s. The scene dims everything but
  the two lamps and labels the reference "Compared with". When Sync starts,
  the page closes with "Calibration stopped: Sync started". Power never
  changes.
- **During Sync**: no test; the brightness slider changes the running Sync at
  once (each change is saved) and leaving without Save restores it. Colour
  shows "Turn Sync off to calibrate color".
- **A lamp the adapter does not drive** (found or not connected): brightness
  only, saved on Save.

See `docs/design/2026-11-19-light-studio-lamp-card.md`.

## Blink

`POST /api/identify` with `{"lamp": "<registry light id>"}` (for example
`"govee-left"` or `"device-<32 hex>"`) asks the adapter to blink that lamp so the
owner can find it. It has the same local-origin protections as layout writes
(Host, Origin, `Sec-Fetch-Site`, `application/json`). An id that is not a registry
light id returns 404, a malformed body 400; the adapter refuses a light it does
not drive. The answer is 200 `{"accepted": true}`,
409 `{"accepted": false, "reason"}` when the adapter refuses (`lamp off`,
`writer closed`, `sync active`, `busy`, `unavailable`, `brightness unknown`, or
`paused` during the owner's live-output hold) and 503 when the adapter is not
reachable or the blink was not delivered. Only an owner click triggers it.

## Power

The owner switches a lamp on or off in Light Studio as from Apple Home or the Hue
app. The lamp card has a Power switch. A lamp that is off always shows the grey
power mark at its corner, with or without Sync; clicking the mark switches the
lamp on. While a switch runs the mark is dimmed; the requested state shows until
the lamp reports it (at most 8 s), and a refusal shows a toast and keeps the
state. Power is not part of the layout: no draft, no undo.

`POST /api/power` with `{"lamp": "<registry light id>", "on": true|false}` has the
local-origin protections of layout writes. The Studio server reads the adapter's
status and uses the lamp's owner power path:

- A Govee lamp listed by the power authority goes to `/power-request`, the Hue app
  path: under the HomeKit authority it sets the same intent as a HomeKit write
  (Apple Home follows within a second); under the remote owner it needs a live
  grant.
- A native Hue lamp with a bridge proxy goes to `/native-power`, the confirmed
  write to the owner's bridge, which Apple Home and the Hue app read.

Both mark an owner power change, so Sync keeps an owner Off and lets an owner On
back in (see `core/README.md`, "Off during Sync"). A native Hue lamp in a running
Sync is held on by the Entertainment stream, and the adapter stops Sync when such a
lamp reports Off; while Sync runs Light Studio therefore does not switch it off
(`power: false`, `power_reason: "sync"`, a write answers 409 `"sync active"`), and
its card points to "During Sync: Off". `GET /api/live` gives each lamp
`power: true` when it is online and has such a path. The answer is 200
`{"accepted": true}`; 409 `{"accepted": false, "reason": "unavailable"}` for a lamp
without a path (offline, a Govee lamp without a power authority) or `"paused"`
during the live-output hold; 404 for a light the adapter does not list; 400 for a
malformed body; 503 when the adapter is unreachable or does not confirm.

## Connected lamps

`/api/live` lists the enrolled lights in `lights` (`id`, `provider`, `model`,
`zones`, `migrated`) and reports `lamps` for each of them: the six migrated lamps
first, then every light enrolled later. A drawn `device-` row is connected while
that list holds its id, and shows "Not connected" otherwise; the six migrated
lamps are always connected. A connected lamp shows its live light and Sync
problems and can blink and be calibrated; a connected Govee lamp also takes
Warmth, Tint and Low light, stored in its layout row.

Connecting is always the owner's choice; placing a found lamp only adds its
layout row. After placing it, a prompt beside the lamp asks "Connect <name>?"
(Not now / Connect). A row that is not connected offers Connect in the Lamps
panel, and the card of a `device-` row or a migrated lamp has a Connected
switch; switching it off asks "Disconnect <name>?" inline (for a migrated lamp
with the note that HomeKit will show it as not responding). Connect sends
`POST /api/lights/enroll` with `{"id": "device-<32 hex>"}`; a disconnected
migrated lamp connects through the found device that discovery names after its
row (`layout_id`), and the core gives it its migrated id back. Disconnect sends
`POST /api/lights/remove` with the row id. Both have the local-origin protections
of layout writes. The core restarts with the new
light set: the lamp shows "Connecting…" or "Disconnecting…" until `/api/live`
lists it (or no longer lists it), then "Connected <name>" or "Disconnected
<name>"; after 90 s without that change it says the change did not finish. A
refusal is said in words: `sync_active` (stop Sync first), `restarting`,
`enrolled`, `not_enrolled`, `unavailable`, `area_full` (the Hue entertainment
area is full) and `channels_shared` (Hue gives the lamp the same channels as
another lamp). The answers are 200 `{"accepted": true, "restarting": true}` (an enrollment
adds the registry `"id"`), 409
`{"accepted": false, "reason"}`, 400 for a malformed body and 503 when the adapter
is not reachable. Removing a row from the layout does not disconnect its light.

## Objects panel

The Objects button opens one panel for everything in the room: Lamps, Furniture,
Found lamps, the Sync Box and the Hue bridge. Pointing at a row marks its object in
the room (a lamp's outline; a frame round the desk, a nightstand, the display or
the Sync Box), and clicking the row selects it. Furniture lists the desk, the
nightstands (cabinets) and the display with their sizes. The trash on a row, or
Delete on a selected piece, asks in place and removes it; ⌘Z brings it back. Without the desk the
display hangs where it was and the Sync Box lies on the floor under it. Without the
display every strip, the Monitor Strip too, is drawn as a bar at its own place, the
room has no samples and no automatic fade zone, and the Sync Box is not offered.
The Sync Box is listed only in its own room, where the trash on its row (or Delete
on the selected box) removes it from the room after a confirmation; a box in no
room is offered to every room with a display ("Add here"). Another room never
lists it: moving it means removing it there and adding it here.
The display of the Sync Box room cannot be removed ("The Sync Box needs a display
in its room"). "+ Desk" and "+ Display" add a missing one: the desk at the room
axis, the display on the desk or hanging at 110 cm. A new room is empty.

Three tiles under Furniture add a desk, a nightstand or a display; the desk and
display tiles are dimmed while the room has one. Like a lamp, a selected desk,
display or nightstand has a chip whose card shows its name and size and offers
Remove from layout.

"+ Nightstand" adds a 45 × 55 cm cabinet on the first free stretch of floor, right
of the desk or at the room axis. A selected
nightstand has the desk's handles: width knobs on both sides (20–300 cm, kept
centred), a height knob (20–150 cm) and typed dimension labels, and it is dragged
or moved with the arrow keys along the floor. A table lamp placed or moved snaps
to the top under its foot nearest to its height (a nightstand, the desk or the
floor); a saved lamp keeps its height. A lamp standing on a nightstand or the desk
follows that top's height, moves with the nightstand, and drops to the nearest top
under it when the piece is narrowed away or removed. A new nightstand needs free
floor ("No free floor for a nightstand"); a room holds up to 12 desks and cabinets. A display standing
on a cabinet is not drawn yet: such a room opens read-only.

## Found lamps

The Objects panel polls `/api/discovery` every 3 s only while it is open. Found
lamps that are supported, online and unambiguous can be placed with "+" and a
click in the room. They become `device-*` rows with their canonical name, model,
shape and zones, which the server verifies before saving. These rows are
configuration only: they show "Not connected", stay neutral in the room and do
not enroll any physical output. Unavailable lamps show their reason (Offline,
Ambiguous); unsupported devices such as plugs are not listed, and Found shows only
when it lists a lamp or the search fails. Section headings carry no counts, and
lamp rows show no brightness correction.

## Live colours and Sync status

The page polls `/api/live` 50 ms after each answer (about 20 updates a second,
close to the Sync frame rate), with a 1000 ms request
deadline and an independent 1500 ms freshness guard; hidden pages stop polling.
Each enrolled lamp is painted from its emitted zone colours (Govee seven zones
bottom to top, the monitor strip nine, Play one). A lamp is drawn unlit when it
is off, stale, offline, excluded or blocked. In the Sync view the monitor shows a
soft gradient of the strip's colours while Sync is active. The view without Sync
never shows Sync colours: while Sync runs, each lamp that runs it shows a demo
scene instead (a warm white at its correction without Sync) and the screen reads
Demo, as the Sync view does with its demo picture while Sync is off. Telemetry only
repaints existing SVG attributes: it never rebuilds the layout and never saves.

The Sync status in the Sync bar shows Syncing, Waiting for frames, Hue
busy, Not syncing, Sync status unknown, "<lamp> <reason>" or "N lamps not lit"
(with a connected box it follows the table in Sync Box). Unknown reasons fall
back to these labels. Clicking it selects the lamp with the problem. A local sleep
hold (`live-output-hold`) reports Not syncing without contacting the adapter.

## Sync Box

The Sync bar at the bottom centre is one block for everything about Sync: one Sync
status (a dot and its words), one action, short messages ("Connected", "The Sync
Box did not respond") and, when opened, the connect steps or the box details.
Messages and the opened card appear above the bar.

| Situation | Status | Action |
| --- | --- | --- |
| No box | none, or the adapter's status once Sync runs from the Hue app (see Live colours and Sync status) | Connect Sync Box |
| Sync off | Sync off | Start Sync |
| Start sent, box not answered yet | Starting… | Start Sync (disabled) |
| Box runs Sync, first frames not in yet (up to 20 s) | Starting… | Stop Sync |
| Frames arrive | Syncing (green) | Stop Sync |
| No frames after 20 s, or a lamp problem | Waiting for frames, "<lamp> <reason>", … | Stop Sync |
| Stop sent | Stopping…, then Sync off at once | Stop Sync (disabled) |
| Box silent for 15 s | Sync Box offline | none |
| Token revoked | Sync Box disconnected | Reconnect |

With a box, the box decides whether Sync is on: the adapter's health is not
shown while Sync is off, starts or stops (the stream winding down is not news).
Clicking the status selects the lamp with a problem, otherwise it shows the box
name, its address and Forget Sync Box. Sync that ends without a Stop from Light
Studio (the box gave up, or the Hue app stopped it) says "Sync stopped on the
Sync Box".

Without a box the block offers Connect Sync Box and opens two steps inside it:
1 Choose your Sync Box (the boxes that answer an mDNS query for `_huesync._tcp`,
or an IP address; a container without host networking cannot receive multicast)
and 2 Hold the button on the box (a 30-second countdown while the server asks the
box for a registration once a second; the owner holds the button until the box's
light blinks green). Errors appear at their step. Forget Sync Box asks first and
removes the registration on the box when it answers.

A restart of the bridge container (an update, a crash, Docker) while the box
streams makes the box stop Sync (`explicit_stop`), or leaves it reporting Sync on
with no frames arriving; while the bridge warms up the box also drops a new
Start. Only a new Start from the box recovers it. The Light Studio server runs a
guard (`bridge/sync_guard.py`) every 5 s. It sees a bridge restart when the
bridge probe stops answering or its `frames_received` counter goes back. It
stops and starts Sync on the box, up to six times over about ten minutes (Stops
at about 25, 85, 155, 285, 415 and 575 s), with 3, 15 and then 45 s of quiet between
Stop and Start (a box whose stream was lost ignored Starts for 4-6 minutes in
live checks), when:

- Sync stopped up to 60 s before or 30 s after a bridge restart was seen;
- the box runs Sync and the adapter reports `waiting` or `off` for 20 s;
- the box drops Sync within 30 s of a Start from Light Studio.

It never acts while the adapter is down, another app streams, the output is
paused or the box is switching, and it never undoes a Stop made while the bridge
runs undisturbed, here or in the Hue app. The Sync bar shows Restarting Sync… meanwhile and says
"Sync did not come back. Stop and start it again" when the six tries fail
(`recovery: "restarting" | "failed"` in `GET /api/sync-box`).

The box can take several seconds to answer (up to 10 s for a read, 15 s for
Start or Stop). Polls answer at once from the last read while a new read runs,
and a box that answered recently stays as it was until it is silent for 15 s, so
a slow answer never shows Offline.

Only the Light Studio server talks to the box (`bridge/sync_box.py`). It keeps
`ip`, `uniqueId`, `name`, `registrationId`, `accessToken` and the SHA-256 of the
box's self-signed certificate in `sync-box.json` (mode 0600) in the private state
directory. The certificate is pinned when the box is connected; a device with
another certificate never receives the token. A box that moves to another
address is followed when a rediscovered box has the same `uniqueId` and
certificate (one search at most every 30 s).

Routes (local origin only; answers never contain the token or the certificate):

| Route | Body | Answer |
| --- | --- | --- |
| `GET /api/sync-box` | | `{"state"}`: `none`, `connecting` (`remaining` s), `timeout`, `failed` (the token could not be saved), `ready` (`syncing`, `changing` while Start or Stop runs), `offline`, `unauthorized`; plus `name`, `ip` |
| `POST /api/sync-box/find` | `{}` | `{"boxes": [{"ip", "name"}]}` |
| `POST /api/sync-box/pair` | `{"ip"}` | state; 400 for an address outside the local network, 503 when no Sync Box answers |
| `POST /api/sync-box/cancel` | `{}` | state |
| `POST /api/sync-box/sync` | `{"active": bool}` | state; 409 without a box, with a revoked token or while another change runs, 503 offline |
| `POST /api/sync-box/forget` | `{}` | `{"state": "none"}` |

The page polls the state every 3 s (every second while connecting) and stops
while hidden.

## Hardware notes

The H6076 floor lamps have seven physically verified zones behind three Signe
sampling anchors; native monitor streaming has nine verified segments; each Play
has one colour channel. Positions are relative to the monitor and published
through the virtual Entertainment API. The 32-inch monitor uses an assumed 16:9
aspect ratio; Govee is drawn 136 cm tall and Play 25 cm long.
