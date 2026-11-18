# Recoverable installation, upgrades and rollback

Issue: #8. Builds on the managed release from #3
(`2026-11-17-core-managed-service.md`). Backward compatibility of the current
hand-made Mac installation is not required; the Mac is a development host and
the target is the NAS (#9).

## Why

A release directory can be built and supervisor files rendered, but nothing
installs a release, records which one is active, protects durable state across
an upgrade or reverts a failed one. Cutovers so far used hand-copied
artifacts and private backups.

## Goals

- A release candidate carries explicit versions and a compatibility contract.
- Staging verifies the artifacts and validates configuration and dependencies
  before anything changes for the running services.
- Activation and rollback are small, journaled transactions; an interruption
  at any point leaves either the previous or the new installation complete,
  and rerunning the command finishes or reverts it.
- Every activation and rollback snapshots durable state first, so pairing,
  layout, calibration, scene intent and ownership state are never lost.
- Publication and live activation keep separate owner approval gates.

## Non-goals

- No service control. The installer never starts, stops or restarts launchd
  agents or containers; it checks that they are stopped and prints the
  commands. The NAS supervisor is chosen in #9.
- No npm publication and no Homebridge installation of the plugin. The
  release ships the plugin tarball; installing it in Homebridge is a #9 step.
- No Setup Studio change. The stale-browser guard for the layout is owned by
  the Light Studio redesign, which rewrites the layout format and server; the
  contract it must keep is listed below.

## Installation layout

```
<base>/
  config/core.json        service configuration (unchanged format)
  state/                  durable state (state_dir in the configuration)
  releases/<version>/     immutable, manifest-verified releases
  releases/<version>/services/   supervisor files rendered at staging
  current -> releases/<version>  the active release (atomic symlink swap)
  backups/<stamp>-<reason>/      state snapshots, mode 0700
  install.json            transaction journal and history
```

Supervisor files use paths through `<base>/current`, so one pointer swap plus
a service restart switches the adapter, Setup Studio and the gateway modules.
Docker resolves the bind-mount symlinks when the container is created, so the
gateway restart is a recreate.

## Compatibility contract

`manifest.json` gains a `compatibility` object:

- `config_version`: configuration format versions the release reads (`[1]`).
- `state`: schema versions of durable state files the release writes
  (desk layout `3`, HomeKit power intent `1`, scene intent `1`).
- `python`: minimum Python for the archive.
- `gateway_image`: the pinned diyHue image digest.
- `core_api`: version of the adapter API used by the Homebridge plugin.
- `plugin`: name, version and file name of the bundled plugin tarball.

The plugin reads `core_api` from the adapter's `/homekit` status and refuses
an incompatible core with a clear log line instead of writing power.

The plugin tarball is built in Python (deterministic tar, fixed mtimes,
npm `package/` layout) from the files `homebridge/package.json` lists, so a
release needs no npm and stays reproducible.

## Commands

All run from a release archive: `python3 lights-can-communicate-v2.pyz <command> --base BASE`.

- `stage --release DIR`: copies `DIR` to `releases/<version>.partial`,
  verifies every manifest hash, checks the Python version and the
  configuration version, runs the new archive's `adapter --check`, renders
  supervisor files, then renames the directory into place. A staged release
  never changes the active one. Staging an existing version with identical
  hashes is a no-op; a different build with the same version is refused.
- `activate VERSION`: requires stopped components (adapter and bridge do not
  answer locally). Journals the intent, snapshots state, swaps `current`,
  journals completion and prints the start commands.
- `rollback`: requires stopped components. Swaps `current` back to the
  previous release. It snapshots the current state first, then restores the
  activation snapshot only when the two releases declare different state
  schemas; otherwise the current state stays (it is compatible and newer).
- `status`: active and previous version, staged releases, an unfinished
  transaction and the latest backups.

### Interruption

`install.json` records `{op, from, to, backup, step}` before each step and is
replaced atomically (write temporary file, fsync, rename). Every step is
repeatable, so a command that finds an unfinished transaction first completes
it: an interrupted snapshot is redone from its `.partial` directory, an
interrupted restore is repeated from the intact snapshot and the pointer swap
is repeated. The owner already authorized that transaction.
`current` is swapped with a temporary symlink and `os.replace`, so it always
points to one complete release.

### Snapshot scope

Everything in `state/` except logs, request logs and stream journals:
the bridge configuration (pairing and certificate), tokens, desk layout,
calibration, scene and power intent. The five most recent snapshots and every
release that `current`, the previous release or the journal names are kept;
older ones are removed after a successful activation.

## Stale-browser contract (Light Studio redesign)

- Layout writes require `If-Match` with the current revision; a write without
  it is refused (428), a stale revision with 409.
- A schema migration rewrites the stored layout, so its revision changes and
  every page loaded before the upgrade gets 409 instead of overwriting it.
- A layout with an older or unknown `version` is refused, never upgraded
  silently from a write.

## Approval gates

`stage` is safe at any time. `activate` and `rollback` change the live
installation and run only after the owner authorizes that version; this
repository's commands never publish packages.

## Verification

Offline tests build a release, stage it into a temporary base with synthetic
state, activate, interrupt each step by fault injection, rerun, and roll back
across a state-schema change and without one. The plugin test checks the
`core_api` refusal. CI runs them in the isolated diyHue image.
