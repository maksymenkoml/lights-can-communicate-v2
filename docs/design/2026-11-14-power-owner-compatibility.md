# Preserve HomeKit identity while coordinating zoned power

## Observed defect

The right pinned H6076 accessory's serial matches its existing Home tile. During
zoned Sync, Home Off reverted to On. With the pilot stopped, Home Off yielded
three consecutive native Off observations. Sending exactly one seven-zone black
`ptReal` datagram then changed native power from Off to On. A color-only writer
can therefore defeat an independent owner's power command between LAN polls.
The hard-crash recovery check separately passed: startup reclaimed its own stale
native stream and the old OpenSSL child disappeared, with no Box re-handshake.

## Compatibility requirement

The owner explicitly requires existing Apple Home/Homebridge accessories, IDs,
rooms and scenes to remain without re-pairing. NAS currently runs
`@homebridge-plugins/homebridge-govee` 11.39.1. Its public source tag points to
`12f2fe5f829f093681bb47bad410ee3785e65640` (MIT). The light On setter delegates to
`internalStateUpdate`; Off already waits 400 ms before sending the power command.
Accessory UUID generation is in the unchanged platform, derived from device ID.

## Approach

Prepare a version-pinned, optional On-setter wrapper and a local output gate.
The wrapper pauses color before calling the original Off handler. It resumes
only after the original On handler succeeds. It does not create, replace or
re-register accessories. Only the two existing pinned device IDs participate;
other devices use the original handler directly.

An authenticated owner heartbeat grants a short per-device color lease. Missing
heartbeats stop zoned writes while native Hue operation remains independent.
If the coordinator is unreachable, Off waits for old color grants to expire,
then still calls the original power handler. A single serialized queue per
device preserves concurrent Off/On order. Replay/session/freshness checks keep
delayed heartbeats from granting old intent. No generic power-write endpoint
or arbitrary physical destination is added.

Keep this optional in the existing adapter; legacy callers retain their current
interfaces. Exercise the coordinator and wrapper locally before proposing NAS
installation. NAS deployment and its Govee child-bridge restart require a
concrete separate approval; local investigation has changed no NAS config.

Faster LAN polling alone cannot guarantee ordering relative to another writer,
so retain native identity/on gates as an additional boundary rather than using
them as the only source of power intent. A plugin replacement/new bridge would
put existing pairing at risk and is outside this compatibility slice.

## Verification

Cover in-flight color drain, Off latch/lease expiry, delayed old grants, closed
sessions, invalid envelopes, token separation, concurrent Off/On and unreachable
coordinator fallback. A real local integration check must submit intent before
the existing Home Off, hold the real lamp off while the other five continue,
and restore On and video. Do not claim the current NAS On setter is integrated
until the wrapper has actually been installed and physically accepted.
