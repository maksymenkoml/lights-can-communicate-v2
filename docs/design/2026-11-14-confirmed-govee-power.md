# Confirm selected Govee power writes

The owner authorized continuing the pilot autonomously until complete or a real
owner action is required. Existing HomeKit IDs, scenes and the two-device allowlist
must remain intact; the third Govee remains outside control.

NAS logs show Left Off followed by native On about one second later. The pinned
11.39.1 LAN sender resolves when UDP send completes, without a device ack. A lost
Off or delayed implicitly powering color packet therefore looks successful to
HomeKit. The exact cause of the individual missed write is not established.

Extend the existing restricted power-owner response with fresh, identity-verified
read-only power observations for left/right. Do not share the general adapter key
or create a generic hardware command route. Existing requests and the accepted
field remain compatible with the installed client.

The new optional client mode confirms identity-verified native power telemetry after the original setter.
Off stays color-blocked throughout verification; On grants colors only after
confirmation. A bounded retry uses the same existing platform power sender,
bypassing a stale handler cache. Require distinct fresh observations and an Off
settling window to catch delayed reactivation. Unknown observations cannot
authorize retries. A persistently observed opposite state surfaces failure;
coordinator unavailability retains the existing independent Home power behavior
after the established grant-expiry rule. Queued operations and shutdown fencing
remain in force.

The hook enables this mode only for the two selected accessories and aligns its
cache after a verified write. Runtime modules are updated with exact installed
hash guards, independent backup and no-clobber publication; no accessory cache,
Homebridge config, pairing, factory, or private credential changes are needed.

Validation covers dropped Off, delayed color reactivation, On-before-color,
unchanged/fake/stale readbacks, bounded failure, unavailable coordinator,
shutdown, ordering, identity/credential isolation and actual Node-to-Python
loopback. Live confirmation must include both devices during Play, with source
and native state evidence retained separately from optical acceptance.


The exact SDK also has independent debounced brightness/color/temperature/scene
writers. Gate those instance methods for the same two IDs, cancel old debounce
keys on Off, and drain writes already started before sending power. Paired Home
scene properties can arrive before On; defer them one event-loop turn and then
wait for that power operation. Preserve explicit custom-scene On, brightness-zero
Off and the existing nonzero brightness cache. A failed implicit On must permit a
new explicit retry. Async-local writer epochs fence SDK timers on the existing
main and scene services without replacing any services or their identities.

Public-source tests exercise the pinned actual brightness and CT implementations.
The SDK's zone brightness/color/temperature tables contain no H6076 entries.
Live left Off still failed during uncapped zonal Play after these guards were
installed. A later quiet, single-command Mac Off converged within 0.84 seconds.
These observations do not establish optical timing or a firmware root cause;
matched packet-pressure and routing checks remain necessary.


Live logs later showed `Power owner rejected intent` aborting both On and Off
verification. Timer and verification HTTP requests were concurrent; a later
sequence can reach the strict replay guard before an earlier request. Serialize
production heartbeat dispatch, constructing its current mask, sequence and wall
timestamp at dispatch rather than enqueue. Keep lower-level stale-response and
explicit-rejection fences, including bounded shutdown's final Off acknowledgement.
Real HTTP delayed-request tests cover this ordering without relaxing replay rules.

Fresh matching readbacks can refresh existing HAP characteristics without power
commands. On still requires confirmation; unknown, foreign IDs and shutdown are
excluded. Skip this cache refresh while any selected power operation is pending,
so telemetry cannot make the original SDK skip a required writer. A macOS Home
write-error tile remained locally cached across a child restart; relaunching Home
read the correct Off state. No claim is made that refreshing HAP alone clears
all client-local write-error UI.
