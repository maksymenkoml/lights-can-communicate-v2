# Lamp power in Light Studio

The owner switches lamps on and off from Light Studio as a permanent, everyday
feature. Until now Light Studio showed power but never changed it.

## Intent

- Switching a lamp in Light Studio is the owner's own power change, the same as
  the switch in Apple Home or the Hue app: HomeKit, the Hue app and Sync see it
  as owner intent. Sync keeps an owner Off and lets an owner On back into Sync.
- Non-goals: room-wide All on/off, scenes, brightness or colour from Light
  Studio, power of plugs or of lamps not in the layout.
- Anti-goals: a second power path that bypasses the owner-intent records (Apple
  Home would disagree with the lamp, Sync would undo the change), and power
  changes in the layout history (undo must never switch a lamp).

## Behaviour

- The lamp card has a Power row with an On/Off switch.
- A lamp that is off always shows the power mark next to it on the stage, with
  or without Sync. Clicking the mark switches the lamp on; the mark goes once the
  lamp reports on. While the request runs the mark is dimmed; a refusal or
  failure shows a toast with the reason and the mark stays.
- A lamp without a power path (offline, or Govee without a power authority)
  shows its state, but the switch and the mark are disabled with the reason.
- Power changes are not layout changes: no draft, no undo entry.

## Approaches

1. **Studio forwards to the existing owner paths (chosen).** `POST /api/power
   {"lamp", "on"}` in the Studio server picks the path by provider: a Govee lamp
   goes to the adapter's `/power-request` (the Hue app path, which under the
   HomeKit authority sets the same intent as a HomeKit write and otherwise
   queues an owner command), a Hue lamp to `/native-power` (the confirmed write
   to the owner's bridge). Both already mark an owner change for Sync-off. No
   adapter change; Studio keeps only the adapter token.
2. A new adapter route for Studio power. One more authenticated surface that
   duplicates the two paths above; rejected.
3. Studio signing `/homekit` writes. Studio would need the power-owner
   credential and would cover Govee only; rejected.

## Interface

- `GET /api/live` gives each lamp `power: true|false`: whether Light Studio can
  switch it now (online, and a Govee lamp listed by the power authority; a Hue
  lamp with a bridge proxy).
- `POST /api/power {"lamp": <light id>, "on": <bool>}` answers `200
  {"accepted": true}`, `409 {"accepted": false, "reason": "paused" |
  "unavailable"}`, `400` for a malformed body or unknown lamp, and `503` when the
  adapter does not answer or does not confirm.
- The Studio README rule "Writes accept geometry and corrections only: no
  power" changes to name this one power route.

## Left to the builder

- Copy of tooltips and toasts.
- Polling cadence after a switch (the live poll already refreshes power).
