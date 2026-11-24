# Pairing with the physical Hue bridge

Issue: #60. Follows #7 and the rooms design (`2026-11-24-rooms.md`, #78).

## Intent

The core uses a physical Hue bridge only when an operator wrote its credentials
into files by hand. A new owner cannot do that. The owner pairs the bridge from
Light Studio, as the Sync Box is paired today: the core finds the bridge, the
owner presses its link button, and the core keeps the keys as private state.

## Goals

- Light Studio finds the Hue bridge on the LAN (mDNS `_hue._tcp`, or an address
  the owner types) and pairs after a link-button press.
- Pairing stores the application key, the streaming client key, the application
  ID, the bridge address and the bridge ID in one private file. No secret value
  is shown or logged.
- The core verifies the bridge certificate against the Signify root CA and binds
  every connection to the paired bridge ID before it sends a key.
- The core creates and owns its native Entertainment area for the Hue lights of
  the room with the Sync Box. The owner's current area becomes the core's area.
- Re-pairing the same bridge or a changed bridge address keeps the virtual
  resources, scenes, HomeKit identities and light registry.
- An owner without a Hue bridge uses Govee lights only (done in #7).

## Non-goals

- Choosing among existing Entertainment areas: the core only reads areas it does
  not own (rooms design).
- Replacing a bridge with another bridge while Hue lights are connected.
- The first-run flow (#59).

## Decisions

- Pairing runs in Light Studio, which already holds the Sync Box pairing, and
  writes `hue-bridge.json` (mode 0600) in the state directory. The adapter reads
  it when the service configuration names no Hue files of its own. Explicit Hue
  files in the service configuration still win, so the current installation
  keeps working unchanged until its operator removes them.
- A paired bridge with no Hue light and no area is valid: the adapter starts,
  discovery lists the bridge's lights, and Sync drives Govee lights only. Hue
  lights can be connected after stage 2 creates the area.
- After a pairing, Light Studio asks the adapter to restart, so it reads the new
  bridge.
- Pairing a bridge with another bridge ID than the pinned one is refused while
  Hue lights are connected.

## Stages

1. Pairing: find, pair after the link button, store the keys, adapter start with
   a paired bridge and no Hue light, Light Studio pairing card.
2. The core's own Entertainment area: create it for the first Hue light of the
   Sync Box's room, adopt the existing area.
3. TLS everywhere: the relay, the proxies and discovery verify the Signify root
   CA and the pinned bridge ID; the core follows a changed bridge address by its
   ID.

Each stage is a separate PR, verified live on the NAS before it is offered.

## Stage 2 decisions

- The core's area is the one in the light registry. An existing pairing's area
  is adopted as it is; nothing is renamed or moved.
- Without an area, the first Hue light creates one: name "Light Studio",
  configuration type `screen`, that light as its only member. A creation that
  cannot hold the light on its own, or an enrollment that fails afterwards,
  deletes the new area again.
- A registry area that the bridge no longer has is replaced the same way, but
  only while no Hue light depends on it. Otherwise the enrollment is refused as
  unavailable.
- The area stays when its last Hue light leaves, so a later light joins it
  again. Choosing the area per room follows with the rooms work (#78).

## Stage 3 decisions

- One verified connection (`HueBridgeLink`) serves every client that sends the
  key: area, light proxies, relay area requests and discovery. It verifies the
  Signify root CA and the pinned bridge ID before any key leaves the core.
- A changed address is followed by the pinned ID through mDNS from the adapter's
  LAN address, at most every 30 seconds, and kept in memory only. Configuration
  files are not rewritten; after a restart the configured address is tried
  first.
- The DTLS stream needs no certificate check: the client key authenticates both
  ends. It uses the link's current address.
- The legacy prototype CLI (`lights_can_communicate_v2.py` commands) is not part
  of the managed runtime and keeps its own client.
