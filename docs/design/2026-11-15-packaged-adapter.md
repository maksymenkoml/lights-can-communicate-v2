# Packaged adapter: first production transition component

The approved transition is incremental. This slice makes the existing Python
adapter a reproducible, executable `.pyz` artifact with a version-one service
configuration. Python 3.10+ and OpenSSL for native video remain host dependencies;
no package publication or NAS deployment is part of this slice.

## Contract

- Preserve existing adapter flags, HTTP contracts, device selection, calibration,
  identity checks, power authority and persisted data formats.
- A new launcher resolves all paths relative to its configuration file, never
  the current working directory. The state directory explicitly selects the
  existing hold and layout files. The old script defaults remain compatible.
- Configuration contains paths to existing credentials, not credential values.
  Reject unknown keys, duplicate JSON keys, unsupported versions, malformed
  booleans/ports/addresses, missing required files and inconsistent dependencies.
- `--check` validates local configuration, dependencies and imported runtime
  without sockets, device access, token creation or state writes. It is not a
  device-health check. Output and errors must not disclose credential contents.
- The packaged launcher requires the existing power coordinator credential.
  Startup hold must still prevent output before networking is initialized.
- Gradient mode is configured before importing the runtime. Native video requires
  a usable OpenSSL dependency; use the existing proven binary selection.
- SIGTERM follows the adapter's existing orderly stop path; authenticated status
  identifies the packaged version. No new power commands are introduced.
- Build only explicitly selected public Python files with fixed archive metadata.
  No configuration, secrets, state, tests, arbitrary files or archives are bundled.
  Refuse to overwrite an existing artifact. Same source produces identical bytes.
- The artifact runs with isolated Python from outside the repository. Source-tree
  imports remain compatible, but the artifact must not search a repository path.

## Scope decision

This first artifact remains the pinned desk adapter, including its existing
native Hue role/area constants. It is not a generic release: moving those IDs
into a versioned device profile is the next component. Do not claim this artifact
contains no identity constants; it contains no real configuration or credentials.
This narrows the broader production design's identity-free release requirement
until the profile migration is independently complete.

## Verification and activation

Use synthetic configuration for archive execution and contract regressions.
Run the packaged process in the pinned network-isolated Linux fixture for real
HTTP and signal lifecycle checks, with no host ports or private mounts. Run
existing protocol/power/recovery tests against archive modules as well as the
normal source suite. Review before any live replacement. Local pilot replacement
must fence the old process before starting the artifact, preserve existing paths
and authority, and retain a concrete old-command rollback. NAS remains unchanged.
