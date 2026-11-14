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
