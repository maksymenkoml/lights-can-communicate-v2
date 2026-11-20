# Sync Box control in Light Studio

Status: approved by the owner on 2026-11-19.

## Intent

The owner starts and stops Hue Sync from Light Studio instead of the Hue app.
When no Sync Box is connected, Light Studio connects one. Nothing else of the
Sync Box (HDMI input, mode, intensity) is in scope; the Hue app keeps those.

## Goals and non-goals

- Goals: a Sync Box corner with Start Sync / Stop Sync; Connect Sync Box with
  discovery, a manual address and the box's button press; Forget Sync Box.
- Non-goals: HDMI input, mode, intensity, brightness, several boxes at once.
- Anti-goals: the access token reaching the browser, a log or the repository;
  talking to a device that only claims to be the paired box; changing lamps.

## Approaches

1. **Light Studio server owns the box client (chosen).** The server already
   proxies the adapter. The token stays on the server, next to the other private
   state, and the adapter (the lamp-safety process) is not touched.
2. Adapter owns the box client. Couples Sync control to the process that is
   restarted for lamp work and has to stay small; no benefit for the UI.
3. Browser talks to the box. The box uses its own TLS certificate and needs the
   token, so the token would reach the page. Rejected.

## Design

Owner choice: the corner at the top right of Light Studio (mock variant C).

- **Corner.** No box: "Connect Sync Box". Connected: the box name, a dot (green
  while Sync runs) and Start Sync / Stop Sync. Unreachable: "Sync Box offline"
  without the button. A revoked token: "Reconnect".
- **Toolbar.** The existing view switch is renamed "Settings": it shows the
  Sync or the without-Sync settings and never starts Sync.
- **Connect card** (lamp card look): boxes found on the network, an address
  field when none is found, then "Hold the button on the Sync Box" with a
  30-second countdown. The server repeats the registration request until the
  box answers with a token or the time runs out.
- **Box card** (click on the name): address and "Forget Sync Box" with a
  confirmation. Forget removes the registration on the box when it is reachable
  and always removes the local file.

### Server

- `bridge/sync_box.py` (stdlib only): mDNS query for `_huesync._tcp.local`,
  HTTPS to the box on port 443, the registration loop, execution reads and
  writes. The box certificate is self-signed: the SHA-256 of its DER form is
  pinned when the box is connected, and every later request refuses another
  certificate. If the box moves to a new address, a rediscovered box with the
  same `uniqueId` and the same certificate is followed.
- Private state: `state/sync-box.json`, mode 0600, next to `adapter-token`:
  `ip`, `uniqueId`, `name`, `registrationId`, `accessToken`, `fingerprint`.
- Routes (local origin only, like the others): `GET /api/sync-box`,
  `POST /api/sync-box/find`, `/pair {ip}`, `/cancel`, `/sync {active}`,
  `/forget`. Responses never contain the token or the fingerprint.

### Limits

- Discovery needs multicast on the host network. In a container without host
  networking only the address field works.
- Pairing and Start/Stop on real hardware are checked live by the owner;
  offline tests use a fake box transport.

## Left to the builder

Polling cadence, error wording, card animation reuse, test structure.
