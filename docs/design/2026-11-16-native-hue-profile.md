# Native Hue profile extraction: next bounded component

Status: implemented and activated locally with the same selection. Read-only
parity confirmed the owner's four native lights and twelve native channels;
scene, real PS5 Sync and Stop-to-scene passed after the archive cutover.
See `core/evidence/native-hue-profile.json` for evidence and remaining limits.

## Intent

Replace per-instance native light, Entertainment area and member UUID assumptions
with an explicit versioned internal profile. Start with the same four native
roles and twelve native channels so output behavior, HomeKit/Hue identities,
calibration and existing scenes remain unchanged. Additional grid memberships
are not automatically enrolled by extracting the current profile.

## Initial scope

A strict immutable profile describes `strip`, `hue_left`, `hue_center`,
`hue_right`, the native area UUID and the ordered twelve channel/member
UUID/index pairs. Validate exact unique resources and channel IDs/types before
any output. Preserve the current logical channel order and calibration mapping.
Do not select by display name or IP alone.

Pass profile objects to the native client/proxy/relay instead of mutating module
globals. Scene and video must use the same profile. Unknown resource, changed
area membership or channel mapping fails closed. Keep existing ownership and
native-health checks, metadata observers and packet bytes unchanged.

## Compatibility and migration gates

The version-one service configuration can gain an optional profile-file path;
existing source entry points and configuration formats remain supported during
migration. Explicit profiles are authoritative for resource selection; do not
require duplicate edits to an old credential file's source-light hint. Credentials
remain separate and never belong in a profile or diagnostic output.

Generate the owner's first profile from the exact validated current selection
and area metadata, preserving UUIDs rather than creating replacement accessories.
Retain a legacy compatibility path until the explicit profile has passed parity
checks. Removing identity defaults from the distributed artifact is a separate
migration gate; merely moving constants must not be presented as a generic build.

## Acceptance

Use alternate synthetic UUID profiles to prove request paths, membership checks
and frame encoding use injected values rather than legacy constants. Reject
malformed/duplicate/partial profiles before IO; verify scene/video profile parity,
legacy defaults, output observers and shutdown. Then compare the current profile
against live read-only area metadata and perform a bounded same-selection cutover.
Adding/removing arbitrary selected grid devices, generating virtual resources,
Homebridge owner coordination for further Govee and lifting the fixed role count
remain subsequent topology/enrollment work.
