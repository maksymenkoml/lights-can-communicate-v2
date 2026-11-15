# Apple Home power feedback into the virtual Hue bridge

The owner confirmed physical Right Lamp Off and authorized continuing with
matching power state in Hue. Keep existing HomeKit identities and scenes, the
two-device allowlist, animation timing and all physical power writers intact.

Use a read-only authenticated adapter status poll in the virtual bridge. Fresh
NAS owner intent updates the corresponding dummy light and publishes Hue light
and grouped-light events. Feedback never calls a physical power setter. Home On
resubmits the current color target so static and animated scenes can resume.
Off keeps animation workers alive but masks their virtual On state. Expired or
unavailable authority retains the last displayed state; permission expiry alone
does not mean physical power Off. A repeat heartbeat must not undo an explicit
Hue Off. Bidirectional power control initiated in Hue remains outside this slice.

Approaches: pushing from NAS would need another deployment and bridge credential;
polling native firmware alone loses the accepted Off-before-drain ordering.
Polling the existing owner snapshot reuses the approved authority and needs no
NAS change. This implementation choice is the agent's, within the authorized
feedback work.

Validate malformed/expired status, role isolation, duplicate events, restart,
Off during animation, quick Off/On revisions, no physical feedback loop, grouped
state, and static color restoration. Exercise the pinned actual diyHue class in
an isolated container before updating the live reference bridge. Preserve its
identity and pairing when recreating the container.

## Mobile verification findings

Actual iPhone Mirroring tests confirmed both Home On/Off paths, but found two
client delivery failures: a native app scene recall reapplies stored On after
the immediate Off event (the same recall made externally corrects the UI), and
a Home On during Play can disappear despite correct physical/API state.

The pinned upstream SSE broker clears a shared list after 0.3 seconds, while an
active subscriber can sleep for 0.4 seconds. A power event added between those
points is removed before any subscriber reads it. Keep upstream logging/event
publication intact and add a bounded fanout journal before Hue object imports.
Each SSE connection has its own cursor; reading one connection never consumes
another's events. The route emits a current virtual light/group snapshot on a
new connection or replay gap. Snapshot construction uses the existing scene
lock and captures its journal cursor before reading resources, so independent
layout publications during construction are replayed rather than skipped.
Resynchronizing deleted arbitrary resources after history overflow is outside
this selected-light state repair; snapshot updates do not invent delete events.

Keep scene-response Off correction as a separate client ordering repair. It is
coalesced from the latest successful response, requires fresh owner intent, and
is cancelled by Home On. Neither event transport nor correction writes power.
