# Restart recovery for the local Hue pilot

The owner asked for autonomous completion of the pilot edge cases. Existing
HomeKit IDs, scenes, pairing, layouts and power authority must remain intact.
The third Govee remains excluded. These fixes add no UI settings or credentials.

## Evidence

A coordinated inactive UDP2100 test sent six nonce packets after listener
readiness. Neither loopback nor LAN packets reached the container. Recreating
only the bridge restored both routes and real Box DTLS input. Ordinary Stop
then reproduced the missing UDP ingress. The tests do not implicate lamp colors.

Cold recreation retained definitions but left runtime Play workers stopped.
The renderer restored cached gradients; an explicit existing-scene Play recall
was needed to restart animation.

## UDP approach

Keep the existing external UDP port bound throughout idle periods. Prefer a
small SO_REUSEADDR guard over a front relay when actual Linux/OpenSSL routing
proves that authenticated active traffic reaches the worker. The guard only
discards unmatched or inactive packets. OpenSSL retains PSK authentication.
A relay would add packet and peer ownership state without a current need.

Keep the existing Start/Stop generation fences. Shutdown must stop workers
before closing the guard. A shutdown timeout must retain the guard binding.
Enable the guard explicitly in the real bridge bootstrap. Constructor defaults
remain compatible with existing fixtures. Reject unsupported guard platforms
before creating a socket. Bind during hold but disable serving.

## Play approach

Store a bounded mapping of allowed virtual light slots to existing scene UUIDs.
A whole-room last-scene value cannot preserve a manual override of one light.
Do not serialize scene objects, API users, palette commands or credentials.

Explicit Play remembers actual workers. Stop, static recall and manual light
changes cancel only affected intent. Power feedback and Sync pauses preserve it.
Replay uses the current guarded worker factory and layout palette selector.
Skip absent or invalid scenes and slots. Replay must not emit power fields.
Keep a missing file backward-compatible. Write atomically with mode 0600.
Replay workers must be daemon threads. Ordinary API Play retains its caller
thread behavior.

The parent integrates both modules after existing wrappers. Skip replay during
live-output-hold. Existing host power and native-on gates remain authoritative.
No fixture is powered on to validate startup while the current owner intent is Off.

## Validation

Use isolated actual OpenSSL cycles, late idle packets, wrong PSKs, peer changes
and shutdown races. Test cold replay, partial scenes, cancellation, corrupt state
and write failures. Review each module independently. Then run the full suite.
Validate Docker Desktop UDP counters across inactive and repeated sessions while
fixtures stay Off. Verify logical replay after a controlled cold recreation.
Keep optical acceptance, local transport proof and API state as separate claims.
