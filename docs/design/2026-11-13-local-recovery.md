# Local recovery hardening

Continue the approved local pilot investigation before a Homebridge/NAS port.
Preserve the current six roles, eighteen source channels, legacy fourteen-channel
mode, saved layout, owner power, identity gates and per-device packet budgets.
No new user settings or physical device discovery authorization are required.

Use actual loopback HTTP and OpenSSL DTLS for receiver fault tests, with synthetic
credentials and external Hue objects replaced at their boundary. These tests must
never address the live adapter or a physical bridge. Simulated native I/O exercises
the existing output/ownership components separately.

Cover a source pause longer than two seconds, malformed and fragmented frames,
repeated starts, explicit Stop while idle, listener/process failure and subsequent
restart. The host's short lease continues to restore scenes during missing input;
an established source connection should remain usable without a new Box handshake.
Expose idle/recovery/rejection state without raw SSL output or credentials.

Exercise native transport/cleanup failures, source expiry during handshake, owner
off, stale native observations and late frames. Preserve existing interfaces and
defaults unless a regression test demonstrates a recovery defect. Add fixes at
the failing boundary rather than replacing the architecture.

Record automated and live evidence separately. A loopback transport pass does not
prove Sync Box firmware recovery, optical timing or Xbox/HDR coverage. Keep the
working UI and shared layout, and leave the pilot's latest owner power state intact.
