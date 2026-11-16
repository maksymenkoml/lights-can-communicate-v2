# Read-only live output preview in the placement grid

The owner asked for live zone colors and Sync state on the existing grid.
This takes priority over the inspected native-profile extraction; no profile code
was changed. The owner then enabled a sleep hold. The physical adapter is stopped
and `state/live-output-hold` must remain until explicit morning resume. All work
and acceptance in this slice use synthetic data and isolated tests. No physical
adapter, lamp, Box, phone or NAS operation is permitted overnight.

## User-facing behavior

Show the emitted output colors on the six enrolled fixtures, including seven
Govee zones and nine monitor-strip sample zones. Off fixtures have no active glow;
unavailable/stale data and unbound placements remain neutral, never a synthetic
animated palette. These are output-command colors, not optical measurements or
firmware acknowledgement. Govee LAN cannot read back all emitted segment colors.

The status badge distinguishes active Sync, waiting for video/output, off,
external native streaming, unavailable state, and the explicit sleep pause.
Status must use freshness/output readiness; a retained idle receiver session is
waiting, not proof of streaming. No new control buttons or settings.

Preview refresh is read-only and never autosaves. Update SVG color attributes
without rebuilding the scene, changing geometry, losing pointer capture, or
replacing focused controls. Use bounded polling, smooth CSS color transitions,
reduced-motion support and visibility-aware suspension. A failed/hung refresh
must expire visible data rather than leaving a false live indication.

## Backend contract

A shared passive OutputPreviewStore observes successful actual output operations.
It is shared across scene and video paths, so mode changes cannot resurrect an
older controller's color cache. Metadata failures must never affect send results,
write ordering, packet pacing, ownership, shutdown or permissions. New optional
observer arguments preserve existing source API compatibility.

```
OutputPreviewStore(clock=time.monotonic)
store.wrap_govee(sender, source) -> sender  # source scene-output/sync-output
store.record_hue(role, body)               # accepted calibrated scene PUT
store.record_native_channels(channels)    # successfully emitted native RGB16 frame
store.snapshot()                          # copied internal normalized output data
build_preview(adapter_status, output_snapshot) -> public preview
```

The adapter appends `preview` to authenticated `/status`:

```
{"version":1,"mode":"scene|sync|waiting|foreign",
 "lamps":{"govee-left":{
   "online":true,"on":true,"brightness":65,
   "colors":[[166,0,0]],"source":"scene-output","stale":false
 }}}
```

Use existing six layout IDs only. RGB triples are bounded integer bytes with
brightness/calibration applied exactly once. Govee colors come from dispatched
colorwc/ptReal plus brightness commands, not pending jobs. Hue scene colors retain
accepted color state across dimming-only deltas and clear obsolete color modes.
Native video uses actual post-calibration channels (center0,left1,right2,strip3..11)
after successful emission. Failed sends do not advance the cache. No raw keys,
addresses or physical IDs in the public preview. Off and stale metadata masks glow.
Govee metadata validity uses existing identity verification/status age<=0.75s;
native Hue uses healthy native age<=2s. Active video requires ready output and
fresh frame age<=0.5s. Expired video colors cannot be advertised as live scene.

## UI gateway contract

The local server projects only bounded metadata to `/api/pilot`:

```
{"version":1,"connected":true,"mode":"scene|sync|waiting|foreign|unknown|paused",
 "sync":{"state":"active|waiting|off|foreign|unknown|paused"},
 "lamps":{"govee-left":{"online":true,"on":true,"brightness":65,
   "colors":[[166,0,0]],"source":"scene-output","stale":false}}}
```

Preserve existing connected/mode/lamps metadata fields additively. Adapter output
preview supplies colors; never invent a fallback palette from target geometry.
A small shared cache reads only virtual `/probe/status` receiver activity when
needed to distinguish Sync off from an idle requested session. It does not read
physical Hue/Govee devices. Cache lifetime is bounded; errors produce unknown.
A local sleep hold returns paused without contacting the stopped adapter/source.
Adapter unavailability produces neutral expired data, not an On/Off assumption.

## Boundaries and proof

New identity-bound layout items remain configuration-only and receive no fake
live colors. The physical writer and its sleep hold remain untouched. Keep all
existing file formats/IDs, autosave, selection, discovery and keyboard behavior.
Test output parsing/brightness, mode changes, partial/failed sends, observer
failure isolation, stale/Off/unknown masking, source idle/freshness, sleep no-IO,
secret-free projection, polling failure/timeouts, drag/focus safety and no saves.
Run source and archive contracts in isolated Linux. Prepare an artifact but do not
activate it or remove the owner's hold. Do not open the owner's layout tab.
