# Entertainment relay pilot

The owner authorized building and testing video sync on the current pilot before deciding what to port to Homebridge. The approved path is Sync Box -> virtual Entertainment bridge -> pinned Govee LAN plus genuine Hue Entertainment stream.

Use a separate opt-in virtual desk Entertainment area with six logical lights and fourteen channels: two Govee, nine monitor-strip segments, three Play. Match the native four-light area and its twelve channels by explicit selected resources. Preserve all current room/scenes and Apple Home power ownership. Keep Govee calibration 0.65, center Play 0.50, other Hue 1.00.

Implement an authenticated frame boundary between container and host. The container receives DTLS and forwards validated RGB/xy frames without physical protocols or real Hue credentials. The host arbitrates scene/video output, rejects stale/replayed/unknown frames, and expires video on missing frames. Only the two Govee IDs and four native Hue IDs are writable. Black video frames use black RGB, never power commands. Start requires the selected Hue lights already on; owner off aborts the native relay. Stop releases only our owned native session and resumes the still-running scene state.

The genuine bridge relay uses its dedicated Entertainment stream rather than REST writes per frame. A dedicated local credential is paired when the owner presses the bridge button; the credential stays in a host-only ignored 0600 file outside Docker mounts. An existing foreign native stream must be left alone.

First prove parser, channel mapping, calibration, priority, expiry, and cancellation offline. Then prove encrypted reception without physical output before linking the owner's Sync Box to this test area. Validate video colors/latency, black frames, Stop/resume, power handling, and reconnection physically. Keep feature status explicit if Sync Box compatibility prevents the next stage. Do not change HDMI wiring, reset devices, deploy on NAS, or publish a package.
