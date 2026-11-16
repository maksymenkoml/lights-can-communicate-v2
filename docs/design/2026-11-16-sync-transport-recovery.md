# Intermittent Sync transport and bounded diagnostics

## Reproduced failure

Read-only samples during the owner's PS5 game showed the receiver continuing to
decode frames while its HTTP outbox recorded network failures and stopped
forwarding. Host-local status reads remained responsive. The adapter's 0.5-second
fresh-frame lease expired, released native output and restored the saved scene;
native release/restart then added further waiting. A separate container-to-host
GET probe reproduced network errors with approximately 300 ms elapsed time. The
last address failure was ENETUNREACH; dual-stack fallback can obscure the first
attempt, so this is evidence of transport failure rather than proof of one
specific kernel or DNS fault.

## Changes

Reuse one authenticated HTTP connection for sequential latest-frame messages,
instead of opening a new connection per frame. Reuse is permitted only after a
fully consumed and validated stream request. Recheck authentication per request;
close other routes and all error responses. On failure, discard that attempt and
reset the connection; the next attempt uses the latest queued frame. Preserve
0.5-second frame-envelope freshness, sequence/session fences and explicit Stop.

Retain established output ownership for at most two seconds from the last
accepted frame. Expired frames are neither returned by the lease nor rendered;
existing physical colors remain while scene output is paused. Fresh frames resume
the same native connection. Explicit Stop, shutdown, unsafe native metadata and
confirmed native Off release immediately. Native Off keeps the existing blocked
session requirement. A longer gap restores the scene. The UI continues to report
waiting for stale data rather than claiming active video.

Add sanitized outbox error categories, failures, coalescing/expiry counters,
send latency, successful-forward gaps and connection counts. Receiver frame age
and adapter transition counters connect the two stages. Both processes keep 64
recent transitions and separate private JSONL journals, each limited to 256 KiB
plus one rotated backup. Only fixed reasons and numeric metrics are retained;
never persist credentials, payloads, addresses or exception messages. Journal
failures must not change output. No additional UI settings or NAS changes.

## Acceptance

Test connection reuse and every-request auth, malformed/error responses,
reconnection without replay, short-gap hold without stale sends, long-gap
release, Stop/Off/shutdown precedence, telemetry sanitization/rotation/failure
isolation and legacy source/archive contracts. Verify public source in isolated
Linux. After a bounded local artifact/container cutover, collect a matched live
PS5 time series and report actual transport errors, frame gaps and output
transitions separately. Preserve rollback artifacts, pairing, power state, IDs,
calibration, layout and existing Homebridge scenes.
