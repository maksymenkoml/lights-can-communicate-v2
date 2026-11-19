# Sync wait reasons and sanitized diagnostics

Issue: #15 (backend slice).

## Why

Light Studio shows one Sync label (`active`, `waiting`, `foreign`, `off`,
`unknown`). During live work the operator could not tell whether Sync was
waiting for source frames, a native Hue state, a power-owner fence or cleanup.
The adapter already keeps the needed facts (video lease counters, classified
stream transitions, native proxy snapshots, power-owner roles). They are only
spread across the raw `/status` payload, which also carries device and resource
identifiers.

## Goals

- One derived, verified Sync health summary: state, classified reason, frame
  freshness, output health and current blockers.
- Unknown stays `unknown`: a reason is reported only when the adapter's own
  classified transition or a fresh, healthy reading proves it. A missing frame
  alone never becomes a cause.
- A bounded diagnostic export that correlates source frames, forwarding,
  native output and power-owner state, without credentials, IP addresses,
  device IDs or Hue resource IDs.
- The existing setup projection gains `sync.reason` and `sync.blockers`; its
  existing `sync.state` semantics are unchanged.

## Non-goals

- No Light Studio UI change (a separate session owns the redesign).
- No new counters; the power-change history below is the only new log.
- No change to rendering, recovery, power ordering or live output.

## Design

- `bridge/sync_health.py` (pure, standard library):
  - `derive(status)` reads an adapter `/status` dictionary and returns
    `{"version": 1, "state", "reason", "detail", "frames", "output",
    "blockers"}`. `state`/`reason` come from the latest classified video
    transition; without one both are `unknown`. Blockers are listed only for
    selected roles (composition `enabled_roles` when present):
    - `owner_off` – power owner requests Off for a Govee role.
    - `color_fenced` – owner requests On but color permission is closed.
    - `native_off` – a fresh, healthy/identity-verified reading says Off.
    - `native_stale` – reading unhealthy, unverified or older than its limit
      (Govee 0.75 s, Hue 2 s, matching the setup projection).
    - `native_foreign` – a Hue light streams while this adapter does not own
      the native area.
  - `export(status)` returns a bounded, allowlisted copy: service version,
    the health summary, video counters and the existing classified events,
    `last_safety_failure` without resource IDs, power-owner roles, Govee rows
    keyed by role, and native Hue rows keyed by role.
- Adapter `GET /diagnostics` (same adapter-token authorization as `/status`)
  returns `export(status)`.
- Setup Studio `project_status` adds `sync.reason` and `sync.blockers` from
  `derive`.
- `sync_health` joins the adapter archive module lists and contracts.

## Power-change history (follow-up, 2026-11-18)

The first export carried only the current power-owner state, so a Sync stall
could not be lined up with an Off, a lapsed owner heartbeat or a Sync hold.

- `bridge/power_history.py` keeps a bounded (64) list of gate
  changes per authority: `timestamp` (wall clock, the stream journal's format),
  `role`, `cause`, `requested_on`, `color_allowed`, `sync_hold`. An unchanged
  gate is not recorded, so heartbeats do not fill the list.
- Causes: `owner`, `grant_expired`, `intent`, `confirmed`, `adopted`,
  `sync_hold`, `sync_release`, `sync_restore_failed`, `owner_power`.
- `PowerOwner` records after each heartbeat, hold and release. A grant that
  lapses without a heartbeat changes nothing in memory, so it is recorded when
  the authority is next used or read, dated at the expiry time.
- `HomeKitPower` records at every revision change.
- Both `snapshot()`s add `events`; `export()` adds `power_events`, keeping
  only known roles and causes and boolean fields. No device or network
  identifiers enter the list.

## Builder's calls

- Freshness for "frames fresh" is 0.5 s, the threshold the setup projection
  already uses for emitted video.
- Blocker order is stable (role order, then code) so exports diff cleanly.
