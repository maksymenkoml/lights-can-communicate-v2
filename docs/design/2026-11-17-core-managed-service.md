# Managed core service distribution

Issue: #3. Owner decisions (2026-11-17): run on the Mac now with a format that
can move to the NAS later (#9 does the move); backward compatibility of the
old launch methods is not required.

## Why

Only the adapter is packaged. The virtual Hue bridge runs as a `tmp_` Docker
container with `restart: "no"` whose code is bind-mounted from a development
worktree, Setup Studio reads its credential through a checkout-relative path,
and the running adapter was started by hand. On 2026-11-17 a Docker restart
stopped the bridge and nothing restarted it; Sync stayed down unnoticed.

## Goals

- One versioned distribution that runs without a checkout: adapter, virtual
  bridge code and Setup Studio.
- Every runtime path comes from the service configuration: configuration,
  state, logs and the installed release.
- Supervisors restart crashed components and start them at login/boot:
  launchd for the adapter and Setup Studio on the Mac, Docker with a restart
  policy for the bridge.
- A readiness check that verifies the adapter and bridge answer locally.
- Orderly shutdown keeps the existing behavior (adapter SIGTERM finalizer,
  bridge receiver cleanup) and the owner `live-output-hold` still blocks
  output.
- Offline tests build the distribution and exercise it with synthetic state.

## Non-goals

- No NAS deployment (that is #9) and no change of HomeKit, Hue pairing,
  device IDs, calibration or power ordering.
- No Light Studio UI change.

## Design

### Distribution

`python3 -B -m lights_can_communicate_v2_core.dist --output DIR` creates a new directory
`DIR` (never overwrites) containing:

- `lights-can-communicate-v2.pyz` – the existing deterministic archive plus the Setup
  Studio server module and its static assets.
- `gateway/` – the bridge modules that today are bind-mounted (single source
  of truth: `GATEWAY_MODULES` in `lights_can_communicate_v2_core/dist.py`).
- `manifest.json` – version and SHA256 of every file.

### Command line

`lights-can-communicate-v2.pyz [adapter|setup|ready|services] --config FILE`:

- `adapter` (default, so `--config FILE [--check]` keeps working).
- `setup` – serves Setup Studio from the archive on `setup.bind:setup.port`
  (default `127.0.0.1:19881`), reading the adapter token and layout from the
  configured paths.
- `ready` – exits 0 only when the adapter `/status` and the bridge
  `/api/config` answer locally within a bounded time; prints a small JSON
  summary without credentials.
- `services --release DIR --output OUT` – renders supervisor files with
  absolute paths: `com.lights-can-communicate-v2.adapter.plist`,
  `com.lights-can-communicate-v2.setup.plist` and `gateway-compose.json` (Compose accepts
  JSON, so no YAML dependency). Existing output files are never overwritten.

### Configuration

The version-one configuration gains two optional sections:

- `setup`: `bind`, `port`.
- `gateway`: `lan_ip`, `mac`, `container_name` (default
  `lights-can-communicate-v2-gateway`), `image` (pinned digest default), `adapter_url`,
  `live_output`, `hue_strip`, `hue_play`, `entertainment`, `gradient`,
  `timezone`.

`state_dir` remains the single durable state directory; the bridge mounts it
as its configuration directory, as today. Logs go to `state_dir/logs`.

### Supervision

- launchd agents: `RunAtLoad`, `KeepAlive` restart on non-zero exit only,
  `ThrottleInterval` 10 s, stdout/stderr to `state_dir/logs`. The interpreter
  is the absolute Python path used when rendering, run with `-I -B`.
- An active `live-output-hold` makes `adapter` exit 0 with a message, so the
  hold is respected without a restart loop.
- Bridge compose service: `restart: unless-stopped`, pinned image, read-only
  module mounts from the installed release, state mount, the existing ports,
  and a Python health check against `http://127.0.0.1/api/config`.

### Activation on the Mac (after merge, separate step)

1. Build the release into `~/Library/Application Support/LightsCanCommunicateV2/releases/<version>`.
2. Stop the hand-started adapter and the current bridge container; copy (not
   move) the live state directory to the managed state location, keeping the
   old one as rollback.
3. Render services, load the launchd agents, start the bridge compose
   project, run `ready`, then verify Govee/Hue power states, the NAS owner
   heartbeat and Hue CLIP On/Off as in the #22/#23 acceptance.
4. Rollback: unload the agents, stop the new bridge, start the previous
   adapter archive and bridge definition against the old state directory.

## Builder's calls

- Positional command with `adapter` as default instead of a breaking rename.
- Compose output as JSON to stay within the standard library.
- The legacy `experiments/hue-bridge/compose.yaml` stays for development; a
  test keeps its module list equal to `GATEWAY_MODULES`.
