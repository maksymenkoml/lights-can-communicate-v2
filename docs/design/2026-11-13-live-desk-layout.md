# Live desk layout pilot

Connect the existing simple setup UI to the six allowlisted desk roles. Keep
the 5 cm grid, automatic model defaults, local autosave and manual Play
orientation. The UI remains one schematic, without a Save/apply workflow.

The loopback server validates and atomically stores a canonical layout in the
bridge's private state volume. It accepts geometry and brightness correction,
never physical IDs, IPs, power, credentials or arbitrary destinations. Extra
virtual drawings remain unbound. Reject cross-origin writes and DNS rebinding.

Coordinates are front-wall light positions relative to the monitor. The
monitor strip keeps its nine U-shaped sampling points. Each Govee has three
Signe anchors over its 136 cm height, each Play one center. Preserve all
eighteen channel IDs. Clamp Hue coordinates to its normalized range. Play
orientation changes its schematic, not its one-channel capability.

Publish changed channel positions through the virtual area's API and SSE.
Verify whether this Sync Box consumes position updates while streaming;
do not claim instantaneous hot sampling until measured. No native area
membership, pairing or power writes are part of geometry updates.

The same private layout provides brightness corrections. Default corrections
remain Govee65%, Center Play50%, other native100%; preserve the right zoned
packet cap and all fresh-on gates. Gallery scene colors keep Hue's existing
palette behavior; geometry controls Entertainment sampling.

First browser load imports the owner's saved browser layout if the server
has no layout. Thereafter the server copy is authoritative across reloads.
Serialize/coalesce autosave requests. Keep local drafts when the service is
unavailable and report actual save errors without persistent instruction text.

## Measured result

The owner confirmed preservation of the browser layout after importing it to
the server. The actual Sync Box accepted live sampling changes: baseline
left/right seven-zone targets differed in 20/20 samples; co-locating only the
left position with the right produced identical targets in 25/25 samples.
After restoring the original layout, settled targets differed in 20/20 samples.
The server layout matched the archived original exactly after the check.
This verifies source sampling, not optical latency or all HDMI modes.

The initial run had source transport stalls even with old coordinates.
The owner recovered streaming by toggling the Sync Box's upper switch and
restarting Sync. A subsequent position event coincided with another stall.
Geometry events now carry the complete Entertainment resource, matching the
base bridge's event shape; the controlled change and restoration then completed
while source DTLS frames continued. These observations do not isolate every
cause of the Box's earlier transport stalls. Diagnostics retain only static
DTLS stage/error counters, never raw OpenSSL output or credentials.
