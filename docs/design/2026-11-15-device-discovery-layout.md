# Device discovery and real bindings in the placement layout

The owner confirmed that “scene” means the grid placement layout, not an
individual Hue color preset. Replace the synthetic add-shape menu with discovered
physical lights and explicit selection. Preserve the existing six roles and
saved geometry/calibration. Adding a newly discovered item in this slice binds
its identity to a saved placement; it does not enroll another hardware writer.
Generic scene/Sync enrollment and removal of the pinned driver topology remain
the next component. Do not imply that a new placement already receives colors.

## Discovery boundary

The running adapter is the sole Govee UDP reply-port owner. Observe its existing
scan replies; never create another discovery socket or send control/status
commands to an unselected Govee. Discovery never grants output permission.
Read light/device/bridge metadata from the already paired local Hue Bridge on
a bounded background poll. No cloud discovery, credentials in browser responses,
new pairing or uncontrolled native-area import.

Use stable deterministic `device-<32 lowercase hex>` catalog IDs, derived from
Govee identity or Hue bridge identity plus light resource identity, never IP/name.
Retain changing addresses only internally as observation hints. Ambiguous Govee
identity/address reports cannot be selected. Bound catalog size and metadata;
mark stale/unavailable observations without removing saved placements.

Initially recognized profiles: Govee H6076 (floor, 7 zones, 136 cm); native Hue
Play 440400982841/440400982842 (bar, 1 zone, 25 cm); PC gradient strips LCX005/
LCX006 (strip, 9 Entertainment zones). Other observed models remain unsupported;
smart plugs cannot be added as colored lamps. Profiles describe configuration
capability, not authorization for output.

## Local API contract

Authenticated adapter `GET /discovery` returns:

```
{
  "connected": true,
  "devices": [{
    "id": "device-<32 lowercase hex>", "provider": "govee" | "hue",
    "name": "bounded label", "model": "bounded model",
    "shape": "floor" | "strip" | "bar" | null, "zones": 7,
    "supported": true, "online": true, "selectable": true,
    "ambiguous": false, "layout_id": "govee-left" | null,
    "height_cm": 136 | null, "width_cm": 25 | null
  }],
  "providers": {"govee": "ready", "hue": "ready"}
}
```

`layout_id` maps only the already enrolled six identities to their existing role
IDs; it prevents duplicate placements. No IP, physical ID or credentials are
required in the UI projection. The setup server proxies this as `/api/discovery`
with its existing local-origin/Host checks. Provider failures remain recoverable.

## Layout compatibility

Keep version 3, the required six legacy roles, existing centimeter grid/monitor/
desk rules, old virtual drawings, autosave revisions and concurrency merge.
New real placement rows use the catalog ID directly and carry bounded canonical
`name`, `model`, `shape`, `zones` metadata plus existing geometry/scale fields.
The setup server verifies newly introduced catalog IDs against current selectable
metadata, rejects duplicate physical/legacy placements, and retains already saved
bindings offline. Do not query discovery during snapshot/render or require a
reachable device just to edit an existing placement. Driver role lookups continue
to use only the existing six roles in this slice.

## UI

The add button opens a compact list of discovered lights with checkboxes and
“Add selected”. Existing placements show “Already on layout”; unavailable and
unsupported items explain why they cannot be selected. Derive type/zones/size
from profiles; no manual IP/model/type form. Add selected real items once, snap
to the five-centimeter grid, select the new item and autosave. New identity-bound
placements can be removed from the layout without power commands. Preserve
pending/offline saves and field-level merge. Escape all discovered labels.

## Proof and activation

Use synthetic LAN/Hue observations and real loopback endpoints for tests. Cover
IP/name changes, ambiguity, stale entries, unsupported plugs, capacity bounds,
callback failure isolation, canonical bindings, duplicate prevention, offline
editing, autosave and discovery failures. Extend the artifact allowlist and
profile tests. After review, replace the Mac adapter artifact and setup UI process; reload the existing
virtual bridge for its additive layout parser while retaining its private state,
identity and Play intent. Retain output selection and rollback. Do not send color, identify,
power or scene commands to the third Govee or another discovered light. The owner
previously asked to open layout links himself; provide the link without opening
his layout tab.
