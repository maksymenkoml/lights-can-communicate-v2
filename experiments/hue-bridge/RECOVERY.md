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

Cold restart of the virtual bridge remains a separate reliability limit. In the
latest attempt, the Box issued Start but sent no DTLS handshake. The receiver
reached its initial connection timeout; a short, non-reusing UDP diagnostic after
that timeout received no packets. This does not identify the Box firmware or
network as the sole cause, and the source-pause hardware test cannot run until a
new source connection is established.

No NAS deployment, Xbox/HDR coverage, factory reset, or unrelated-device control
is implied by these checks. Existing owner power and discovery allowlists remain
in force. A retained idle source connection does not extend the host video lease.
