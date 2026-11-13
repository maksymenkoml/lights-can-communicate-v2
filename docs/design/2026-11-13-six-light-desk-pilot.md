# Six-light desk pilot

The owner confirmed the monitor strip plus Hue Play Left, Center, and Right as the native Hue lights to combine with the existing two Govee side lamps. This extends the disposable reference bridge pilot, not a packaged Homebridge plugin or NAS deployment.

## Decisions

Use host-side, explicitly pinned native Hue proxies rather than importing the genuine bridge. The genuine bridge key stays on the host. Native power remains owner-controlled through Apple Home/Homebridge; the adapter sends only color and dimming to lights observed on and in normal mode. Streaming or unavailable native state blocks relevant output; all Govee rendering pauses when any selected Hue status is unhealthy or streaming.

A separate Vapor wave DESK scene preserves the original gallery scene. The strip uses five palette points. Left Govee and left Hue Play follow the first point, right lights the last point, and center Play the middle point. Govee brightness keeps the accepted 0.65 multiplier. Physical color balance and strip direction still require the owner's visual check.

## Alternatives

Keeping independent vendor scene engines cannot maintain a shared animation phase. Replacing Homebridge power control would disturb the owner's existing scenes. The existing virtual bridge scene engine with pinned color-only proxies reuses the tested path and retains power ownership.

## Verification

Exercise resource pinning, role routing, transition duration, delta coalescing, off/on restoration, stale and streaming gates, and canceled Play workers offline. Verify exact virtual membership and native target readback live, then obtain visual confirmation of the six-light composition. HDMI Sync remains a separate physical acceptance check. No unrelated Govee or Hue lights are allowed.
