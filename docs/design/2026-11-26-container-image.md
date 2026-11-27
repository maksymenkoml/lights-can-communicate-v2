# Container image install

Issue: #61. Owner decisions (2026-11-26): the core ships as a container image that
an owner creates and updates in Synology Container Manager without a shell or a
root task; the owner's NAS moves to this install and the release-archive path
(installer, rendered Compose project, root operations task) is removed without
backward compatibility. #53 (password) is postponed. #52 later moves the core
under the Homebridge plugin; this install is its rollback target.

## Why

The NAS install works only for the current owner: an operator transfers a
release archive, writes `core.json`, and runs a root script that is not in the
repository through a DSM task. The gateway time zone is fixed in the code. Another
owner cannot repeat this.

## Goals

- Each release publishes `ghcr.io/maksymenkoml/lights-can-communicate-v2:<version>`
  (linux/amd64 and linux/arm64).
- One documented project file and one free LAN address create the core in
  Container Manager.
- A first start on an empty data folder creates the configuration, the internal
  tokens and the bridge identity; Light Studio then opens with no lights.
- Update: change the image version in the project and rebuild. Rollback: the
  previous version. The data folder survives both.
- The time zone comes from the project file.

## Non-goals

- No setup wizard (#59), no password (#53), no plugin pairing by code (#62).
- No change of the gateway's Hue behavior or the light registry.
- Publishing a release (tag) needs the owner's approval per version.

## Approaches

1. **One container, one LAN address, built-in supervisor (chosen).** The gateway,
   the adapter and Light Studio run as child processes of one entrypoint, which
   restarts a failed child. The bridge and the adapter share the address (no port
   conflict: 80/443/2100 vs. the Govee UDP ports).
2. Three services, the adapter and Light Studio in the gateway's network
   namespace (`network_mode: service:gateway`). Rejected: when the gateway
   container restarts, Docker does not reattach the others to the new namespace.
3. Three services with two LAN addresses (today). Rejected: the owner must find
   two free addresses and the project file is long.

## Image

- `Dockerfile` at the repository root, two stages on the pinned diyHue image
  (`config.GATEWAY_IMAGE`): the first runs `dist` to build the release, the
  second copies the archive to `/app` and the gateway modules to `/probe`.
- Entrypoint: `python3 -I -B /app/lights-can-communicate-v2.pyz container`.
- `/data` is the only volume.

## Project file

`deploy/synology/compose.yaml`; the owner edits the values marked in it:

- the image version;
- `CORE_ADDRESS`: the free LAN address (outside the DHCP pool), also the macvlan
  `ipv4_address`;
- the macvlan network: parent interface, subnet, router;
- `TZ` (IANA name);
- the data folder path;
- optional `STUDIO_HOSTS`: extra names or addresses the browser uses.

The container joins two networks: `lan` (macvlan, `CORE_ADDRESS`) and `core`
(bridge `172.30.250.0/29`, fixed `172.30.250.3`). The NAS host cannot reach its own
macvlan containers, so Homebridge on the NAS reaches the adapter on the core
address, and Light Studio is also published on the NAS at port 19881 through it.
Light Studio is reachable at `http://<CORE_ADDRESS>:19881` and
`http://<NAS>:19881` (with the NAS in `STUDIO_HOSTS`).

## Data folder

```
/data/core.json             configuration, version 2 (written by the entrypoint)
/data/state/                durable state (bridge config, registry, layout, ...)
/data/state/adapter-token   internal tokens, 0600; the gateway reads its token
/data/state/power-owner-token   from the state folder it serves
/data/backups/<stamp>-<from>-<to>/   snapshot before a version change (last 5)
/data/version               the version that last started
```

Configuration version 2 replaces the `container.lan` block by
`container: {"core_address", "studio_hosts"}`; the adapter's LAN address is
`gateway.lan_ip`. Every start writes `gateway.lan_ip`, `gateway.timezone` and
`container.studio_hosts` from the environment, so the project file is the only
place to change them. A version-1 file (today's NAS) is converted once on start.

## First start

With no `core.json`: two random tokens, a random locally administered bridge MAC
(the bridge identity, kept forever), an empty light registry (without it the
gateway would create the two legacy Govee lamps), and a configuration with live
output and gradients on, the HomeKit power authority off (`owner`). The gateway
flags `hue_strip`, `hue_play` and `entertainment` stay off: they create the
legacy virtual Hue lights 3–6 unconditionally. Light Studio's own Hue pairing
(#60) provides the Hue bridge later.

Known limit: a Hue light enrolled later gets a virtual light only with
`hue_strip` and `hue_play` on, so on a fresh install it has none yet. Making
lights 3–6 depend on the registry is a gateway change outside this issue.

## Version change

When `/data/version` differs from the image version, the entrypoint copies
`core.json` and `state/` (without append-only logs over 1 MB) into a new backup folder before any child
starts, keeps the last five, then records the version. A rollback reads the
newer state as today's releases do; if a release ever changes a state format
incompatibly, the owner restores that backup folder.

## Supervisor

- Starts the gateway (`/probe/bootstrap.py --docker` with the gateway
  environment and `CONFIG_PATH` set to `data/state`), the adapter and Light
  Studio. The image also has the archive in `/probe`, where the gateway imports
  the core package from.
- A child that exits restarts after 1 s, doubling to 30 s; a child that ran 60 s
  resets its delay. The project sets `init: true` so stray grandchildren are reaped.
- SIGTERM/SIGINT: forwards SIGTERM to all children, waits 15 s, then kills.
  `stop_grace_period: 20s`.
- Prefixes nothing: children write to the container log as today.

## Removed

`install.py`, the Compose rendering in `services.py`, the `services`, `ready` and
install commands, their tests, the release `manifest` compatibility contract that
only the installer read, and the NAS operations script (never in the repository).

## Migration of the owner's NAS

Done once by the agent with the existing operations task, then the task is
deleted:

1. Load the image built from the branch (`docker load`).
2. Build `/volume1/docker/lights-can-communicate-v2/data` from `config/` and
   `state/` (paths rewritten to the data folder).
3. Stop the old project; create the Container Manager project from the project
   file with `CORE_ADDRESS=192.168.10.40`, `TZ=America/Toronto`,
   `STUDIO_HOSTS=192.168.10.10`.
4. Verify: bridge id `020000FFFE000000`, Hue app, Sync Box, Govee replies on the
   new adapter address, HomeKit power, Light Studio on `192.168.10.10:19881`,
   Homebridge on `172.30.250.3:19880`.
5. Rollback until verified: stop the new project and start the old one.

## Verification

- Unit tests: environment parsing, first start, version-1 conversion, backups,
  supervisor restarts and shutdown.
- CI builds the image and starts it on an empty data folder with networking
  limited to the core network; Light Studio and the bridge answer.
- Live: the migration above.
