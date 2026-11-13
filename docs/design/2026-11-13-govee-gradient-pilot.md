# Govee gradient pilot

Represent each selected H6076 as one Signe gradient floor light, keeping its
existing Hue resource identity. Expose three Entertainment anchors per lamp
(bottom, middle, top) and interpolate those colors over seven physical zones.
The physical zone count and order require owner observation on both devices.

Keep the existing single-color pilot as the default. An explicit gradient
mode must agree on bridge and adapter. Keep the original native channel IDs
and add four Govee channels, for eighteen channels across six logical lights.
Gallery gradients and dynamic scenes use the same physical zone renderer.

Use the LAN `ptReal` segment-color format documented by the open source
govee-local-api project, reference commit
`886fc569168be474d727c80ba56ade4d7237a58b`. This is protocol interoperability,
not a dependency on or execution of that library. Never send power commands.
Existing identity verification, fresh-on gates, brightness calibration, packet
pacing, Sync priority and Stop restoration remain required for zoned output.

Implementation sequence: verify packet encoding and interpolation offline;
perform a bounded single-lamp probe with restoration; extend bridge profiles,
scene renderer and streaming topology behind explicit gradient mode; run
regression checks and owner verification before enabling it on both lamps.

The setup sketch remains independent. Layout-to-topology configuration and NAS
packaging are subsequent work, not implicit changes in this experiment.
