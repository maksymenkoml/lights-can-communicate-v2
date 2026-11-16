# Floor-safe placement and an editable Sync zone

## Owner requirements

Draw an explicit floor. Physical fixtures and the complete monitor must stay
above it; the table remains fixed on the floor and only its dimensions are
editable. Reference guides extend across the whole scene, including below the
floor. Functional Sync fade is a closed solid contour with draggable corners and
an actual fading visualization instead of uniform fill. Corner edits affect Sync
and persist. The owner chose attachment to table height and width.

## Zone representation

Optional v3 root `syncZone` contains `{version:1,points:[{u,h},...]}` with six
vertices in perimeter order: top-left, top-right, bottom-right, lower-inner-right,
lower-inner-left, bottom-left. `u` is horizontal distance from table center divided
by half its width; `h` is height relative to tabletop in centimeters. Consequently
table resize follows the saved zone. Missing/null selects automatic geometry.
Explicit null resets to automatic; server preserves an existing custom zone when
an older client omits the field. No fixture ID, composition or calibration changes.

Default world vertices cover desk side edges plus at least50cm lateral margins,
all included fixture centers plus20cm and the monitor width plus20cm. Extents are
rounded outward to5cm. Top points are at tabletop height; lower points retain the
15cm + lateral-gap rule. Automatic geometry thus covers all physical sample x
positions and keeps the existing default field. Once customized, points follow
the table rather than moving whenever a fixture moves.

Validate strict fields/version, six finite bounded points (`u` ±20, `h` ±400),
ordered top endpoints and ordered four lower x positions, a positive vertical
separation from the top line, simple non-intersecting perimeter and x-monotonicity.
UI moves corners on the5cm world grid and retains the last valid shape. Functional
vertices may extend below floor; this is not physical object placement.

For a physical x, intersect the polygon with a vertical line. Its lower/upper
heights define the zero/full-light bounds for smoothstep. Below is zero, above
is full. Outside the custom polygon's horizontal extent there is no desk mask.
Use the existing analytic segment integral/centroid for Govee; use point gains for
Play and monitor-strip anchors. Always sample the original capture palette before
visibility/calibration. Scenes remain unmasked.

## UI and constraints

When 'Межі Sync' is enabled, show solid perimeter and six foreground drag handles;
draw non-overlapping smoothstep-weighted bands following vertical polygon sections.
Reference guides stay thin/dashed and render across the current full viewport.
Extend the viewport for the full functional zone; freeze it during a gesture.
Persist edits through existing autosave/revision/merge/undo, with one Auto reset.
The display switch stays browser-only.

Floor lamps keep minimum anchor60cm, horizontal bars/strips5cm, vertical25cm bars
15cm; monitor floor bounds account its full rectangle/strip. Server canonicalizes
legacy below-floor poses upward. Rotating a bar re-clamps its center. Foot/leg
outlines touch the floor without extending below it. Labels may live beneath the
floor in a shaded non-placement strip. Table drag/arrow input never moves it.

## Verification and deployment

Test invalid/concave/self-crossing zones, relative resize, section interpolation,
default parity, raw colors, corner mouse/keyboard/undo/reset/reload/conflicts,
floor bounds for every orientation, table immobility and unlimited guides.
Review source and run source/archive/Linux/Node contracts. Update core, UI and
the capture container's layout reader together; retain baseline layout/native
metadata and fence writers. No NAS changes or automatic physical enrollment.
