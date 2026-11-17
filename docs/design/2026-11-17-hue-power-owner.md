# Explicit Hue power through the existing Homebridge owner

## Scope

Implement issue #11 for the existing selected Govee left/right roles. Reuse the
current Homebridge setter, native confirmation, color fences and accessory IDs.
Do not deploy, restart services, enroll devices or read private installation state.
Scenes and background animation must never become physical power requests.

## Transport

Use the existing authenticated owner heartbeat as a reverse command channel.
A new NAS listener would require another endpoint and credentials. Calling an
unrelated Homebridge administration API would bypass the existing owner contract.
The heartbeat channel retains one authority and existing outbound connectivity.

Old heartbeat envelopes remain accepted unchanged.
The core advertises `power_request_version: 1` in accepted heartbeat responses.
Updated owners keep the legacy envelope until they receive that capability, so
ordinary Home control also works against an older core.
After negotiation, a capable owner adds:

```json
{"power_requests":{"version":1,"revisions":{"left":0},"results":[]}}
```

`revisions` contains only registered command handlers, is a subset of heartbeat
`states`, and uses nonnegative safe integers. Each explicit selected power call
increments its role revision synchronously, including ordinary Apple Home calls.
A result is exactly `{"id":"<32 lowercase hex>","success":true}`. Limit results
to 64 distinct IDs. Unknown fields, malformed versions, revisions or results fail.

The authenticated core response can add `power_requests` (at most two commands)
and `power_request_acks` (at most 64 result IDs). Each command contains exactly:

```json
{"id":"<32 lowercase hex>","role":"left","on":true,"issued_at":1000,"expires_at":1012,"revision":0}
```

A command expires after at most 12 seconds. Permit the existing half-second clock
skew. Only registered left/right handlers may execute. Reject stale revisions,
expired commands, duplicates, closed clients and malformed envelopes. Commands
call the existing coordinated setter outside heartbeat processing. They must not
await a power operation inside the heartbeat queue. Check expiry and superseding
Home intent again before each physical write/retry and before reporting success.
Success requires existing native confirmation, matching intent and current revision.
Retain bounded results until acknowledged and retain replay fences after ACK.

## Core broker

`POST /power-request` uses the existing adapter token, never the owner token.
Its exact body is `id`, `issued_at`, `states`; IDs are lowercase hex, timestamp
freshness is 0.5 seconds and states are a nonempty boolean subset of left/right.
Queue only when all requested roles have fresh owner leases and advertised support.
Bind commands to the current owner session and role revisions. Keep at most two
in-flight role commands and 64 bounded request records. Retain completed IDs
after the active deadline. Evict the oldest fully completed record only when a
new ID needs space and its accepted payload timestamps can no longer pass the
freshness check. Reject new IDs if every replay fence remains valid. Reject busy
roles rather than overlapping physical power operations. Expired/session-replaced
commands cannot be delivered. Duplicate request IDs cannot create another write.

Wait at most 12 seconds for completion. A success response requires owner success,
the expected next role revision and matching fresh identity-verified native power.
Return an explicit unavailable/failure response otherwise. A group may partially
complete physically; report failure and refresh actual state without rollback.
No remote request directly grants color permission.

## Hue gateway

Process only successful authenticated vendor PUT responses for direct light or
group power updates in v1/v2. Resolve existing light/group membership by identity.
Select only slots 1/2. Never translate scene recall, animation or internal writes.
Exclude v1 scene actions even if they contain an On value. Leave unrelated
accessories and native Hue resources on their existing paths.

Send the selected roles through the core broker. Do not hold the scene lock while
waiting. Refresh virtual state through PowerFeedback after success or failure.
Return a Hue-compatible error when authority is unavailable, unconfirmed or stale.
Respect the existing live-output hold. Keep scene Off masking, existing pairing,
layout, calibration and HomeKit identity unchanged.

## Verification

Use strict schema, freshness, replay, bounded queue, expiry, session and intent
fencing tests. Exercise old owners and unrelated accessories. Run real loopback
Node-to-Python requests with the coordinated setter and synthetic power telemetry.
Use the pinned diyHue image for real API/class parity with networking disabled.
Run all local checks and both CI jobs. Physical device/app/NAS acceptance remains
separate from these offline results.
