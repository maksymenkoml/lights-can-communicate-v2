# Soft desk visibility for Sync

The owner corrected the initial projection: deeply placed Play fixtures must not
be lifted virtually to the tabletop. Sync should extend about 15 cm below the
table, with a broader downward penumbra as a fixture moves laterally away from
the desk's side edge. This rule applies only to Sync; scenes retain full output.

Use the existing Light Studio front-wall geometry, with the desk centered at
100 cm as drawn in the grid. For a source at x, compute its horizontal distance
outside the desk footprint. The fade width is 15 cm plus that lateral distance,
a simple 1:1 initial spread model. It is independent of monitor offset and depth.
No extra UI settings or schema migration are introduced.

The visibility curve is smoothstep from zero at desk height minus fade width to
one at desk height. Integrate it over each of the seven H6076 emitting segments;
the mean is its visibility gain and the brightness-weighted centroid selects its
screen-edge sample. Play uses its saved physical center for visibility and color
sampling, so a deeply under-desk fixture becomes black. Keep included native
fixtures in the owned stream while black, so the previous scene cannot remain
lit underneath Sync. Composition membership, power and native area identity do
not change because of this visibility curve.

Apply visibility to raw RGB16 before existing per-fixture calibration. Old
internal profiles without gain fields retain unit gain. Scene producers do not
call the projection functions. Screen blending for the schematic's glow prevents
dim emitted colors from painting dark shadows over the grid.

Test deep/near/above-desk Play, partial floor segments, whole-band integral and
centroid, lateral symmetry/spread, desk width versus monitor offset, raised lamps,
legacy profiles, unchanged source frame and native membership. Verify full source,
archive and isolated Linux paths, then hot-replace only the local core while the
existing capture stream remains active. Capture the actual native black outputs,
floor gains, owner power and stream freshness; retain rollback.
