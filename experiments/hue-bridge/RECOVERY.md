# Local recovery checks

The pilot is still a local experiment. These results distinguish actual transport
tests, simulated native I/O, and owner/physical acceptance.

## Automated faults

Run the full hook from the repository root:

```sh
./.githooks/pre-push
PROBE_GOVEE_GRADIENT=true python3 -B -m unittest discover -s tests -p test_receiver_recovery.py -v
```

`test_receiver_recovery.py` uses actual OpenSSL DTLS and loopback HTTP, synthetic
credentials and ephemeral endpoints. It never contacts the live adapter or Hue
Bridge. The same sixteen tests passed inside the pinned pilot Docker image with
`--network none`, read-only source mounts, no published ports and no state or
credential mounts, for both fourteen- and eighteen-channel modes.

| Fault | Expected recovery | Evidence boundary |
| --- | --- | --- |
| Established source pauses beyond two seconds | Keep DTLS; host lease expires separately; fresh input resumes same session | Real loopback transport |
| Malformed packet before valid packet | Reject malformed input and resynchronize without dropping DTLS | Real transport and codec tests |
| Partial/coalesced frames and long garbage | Preserve valid framing; bound retained garbage | Codec tests |
| Host HTTP 503 or successful JSON `null` | Keep forwarder alive; accept next fresh frame | Real loopback transport |
| Rejected HTTP response | Close response before retry | Real response stream tests |
| Stop during idle, then new Start | Close listener and use a new forwarding session | Real loopback transport |
| Killed OpenSSL, missing executable, busy UDP port | Clean lifecycle gate; later Start succeeds | Real processes/sockets |
| Wrong PSK | Forward no colors; valid restart works | Real loopback DTLS |
| Overlapping Starts | Finish old listener before opening the next | Real loopback transport |
| API Start accepted before old cleanup, delayed worker | Old cleanup cannot erase accepted intent | Synchronous API boundary plus real transport |
| Stop after Start acceptance but before worker | Never resurrect canceled intent | API boundary test |
| Old spawn finishes after a newer owner is accepted | Do not publish obsolete process or owner | Spawn barrier plus real transport |
| Duplicate queued workers | Keep one listener and retire redundant workers | Real loopback transport |
| Native area/status/owner JSON malformed or unknown | Fail closed as recoverable errors; retain cleanup fence when ownership is uncertain | Simulated native API I/O |
| Native Stop acknowledged without effect or response lost | Confirm area state; retry only our ownership | Simulated native API I/O |
| Source expires or native owner turns off during handshake | Send no buffered frame; release owned stream | Output component tests |
| One Govee off | Keep the other eligible target working; send no power commands | Output component tests |
| Relay write and cleanup both fail | Hold scene fence until release succeeds | Output component tests |
| Delayed Stop from retired source | Cannot stop replacement session | Lease tests |

## Live observations

Restarting the host adapter during the owner's real Sync Box stream resumed video
automatically, with source reception continuing and native output becoming ready.
This confirms process recovery, not optical timing.

An additional hard-crash check killed only the selected adapter with SIGKILL.
The source Box session continued, and its owned native stream remained after the
crash. Startup reclaimed that stream and resumed output on the same Box handshake.
The previous OpenSSL child was no longer present. This is stronger than the
earlier orderly restart observation.

After repairing publication, a controlled four-second pause of only the selected
receiver's OpenSSL process was exercised against the real Box. The process was
resumed in `finally`. Source DTLS remained active, input stopped, and the host
video lease expired. Fresh source frames then resumed and settled output was
confirmed sending again (sixteen additional output frames over the final sample).
DTLS client-hello/finished counters stayed at one: no new Box connection or button
press was required. The owner then confirmed that all six lights react to the
game again. Visual acceptance of the fallback interval and optical latency were
not recorded separately.

The cold restart failure was localized further with a coordinated Start capture.
The Mac received nine inbound UDP packets from the Box at port 2100, but the
container saw none. Both localhost and LAN-self synthetic packets also failed to
reach the container, despite Docker reporting the port published; the Mac firewall
was disabled. Recreating only the selected bridge container restored delivery of
both test packets, verified with raw observation and a directly bound UDP socket.
This identifies a failed Docker Desktop publication path in the observed case;
it does not establish that every Box connection failure has the same cause.

For local bridge code reloads, preserve the existing private state and use:

```sh
docker compose -p smart-lighting-hue-probe up -d --force-recreate --no-deps --pull never bridge
```

Run this from `experiments/hue-bridge`. Preserve the selected opt-in profile in
its private `.env`; this pilot uses `PROBE_GOVEE_GRADIENT=true`. A plain container
restart did not repair the observed stale publication. Do not restart the shared
Docker daemon or grant the bridge access to its control socket for this workaround.
Automatic repair of the external Docker port proxy is not implemented.

The phone's `Ready` label also remained visible while real frames were flowing;
do not use that label alone as proof that Sync is stopped. Validate source and
output counters as well as the actual lights.

## Power race discovered during hardware coverage

The right Home accessory serial was matched to the pinned H6076. Home Off during
zoned Sync did not stay Off in native readback. With the adapter stopped, three
readbacks confirmed Off. One black seven-zone `ptReal` packet then produced three
On readbacks. This directly establishes implicit power-on for the tested lamp;
it is not a claim about every Govee firmware/model.

The no-explicit-power-write policy alone therefore does not guarantee Home Off
when a separate writer continuously sends zonal frames. The prepared optional
[power coordinator](../homebridge-power/README.md) orders permission before the
existing Homebridge power setter. Its local tests do not constitute installation
or physical acceptance on the NAS. Existing Home IDs/scenes must be preserved.

No NAS deployment, Xbox/HDR coverage, factory reset, or unrelated-device control
is implied by these checks. Existing owner power and discovery allowlists remain
in force. A retained idle source connection does not extend the host video lease.


## Source starts before fixture power

An initial source arriving while any selected native Hue fixture is Off waits
without starting the native relay or sending Govee colors. Once all four native
fixtures have fresh, healthy On/normal telemetry, the same source can resume;
no source restart or implicit power command is required. A dedicated regression
covers all-Off, partially-On and all-On transitions on that same source.

This differs from an owner Off during an already started native relay/handshake:
that session remains fenced until an explicit source restart. Power the selected
fixtures while an initial source waits; stopping the source first is unnecessary.

The renewed live attempt received 1,441 host frames while fixtures were Off, but
the operator stopped Sync before powering them. No renewed output acceptance is
claimed. Mirroring subsequently timed out, an existing-client bridge-area Start
probe received no fresh Box frames and returned the area to Stop, and later
fresh telemetry showed all six fixtures Off again. The origin of that later Off
is not established; owner clarification is pending. See
`evidence/renewed-sync-attempt.json`.
