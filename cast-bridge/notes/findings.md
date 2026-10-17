# Bridge findings and handoff state

## Design and scope

Shairport Sync receives AirPlay 2; 48 kHz stereo S32_LE PCM flows through /audio/pcm, FFmpeg encodes 192 kbps MP3, and an HTTP stream feeds Bedroom Display via PyChromecast. No synchronized mixed AirPlay/Cast playback is established. User-observed end-to-end latency is roughly seven seconds, not an instrumented measurement.

Volume uses upstream `general.run_this_when_volume_is_set`: Shairport appends the AirPlay value to a curl POST to the bridge. `ignore_volume_control = "yes"` disables Shairport gain so volume is applied only at Cast. Range -30..0 maps to 0..1 via `(value+30)/30`; -144 mutes; fraction 0 also mutes; invalid/nonfinite input rejected. Current cap is 0.5: clamp, not rescaling. Higher slider settings do not raise device volume above 50%. Host-source-IP allowlisting is not authentication. Concurrent volume requests serialize on a lock.

The real iPhone command was observed at -20.675676 when Walter eyeballed approximately 25%; requested 31.1% and independent device status read 31%. This proves delivery, not exact slider percentage alignment. Later real command -10.684813 mapped to 64.4%, capped to 50%.

## Acoustic observations

Pink noise source: https://www.youtube.com/watch?v=2wgg7KtzTrU . User at seven feet, AirPlay maximum. C-weighted/Slow was the requested method; readings were user-reported, not independently instrumented. Background approximately 49 dB.

| Nest device volume | Before padding fix, dB | After fix, dB |
|---|---:|---:|
| 40% | 51 | not sampled |
| 50% | 51.5 | 51.7 |
| 70% | 55.9 | 55.5 |
| 90% | 60.3 | 60.7 |
| 100% | 63.2 | 63.3 |

Low points are close to background. Do not fit a precise speaker-only curve without accounting for ambient noise and source consistency. Measurements support device-volume behavior, not an exact AirPlay slider law.

## Flutter / silence padding

Old feeder ran an independent 20 ms deadline, performed opportunistic short reads, then padded every incomplete block with zeros. A prior probe suggested about 15% missing PCM versus its independent clock, but that is not definitive loss evidence. The correction accumulates arbitrary fragments, emits only complete blocks, follows Shairport input pacing/backpressure, and retains partial bytes. After a genuine 500 ms input stop it emits silence to keep Cast connected. Fragmented-write unit test preserved 92,160 bytes exactly. Live post-fix samples showed zero inserted silence and Walter reported steady sound. No claim of overall clock correctness or cross-device synchronization.

## Reconnect failures

After receiver recreation, initial iPhone attempts failed with `Attempted to write overlong RTSP packet 3`. A receiver-only restart allowed control connection. Direct GET /info returned 200 but did not validate the complete handshake; root cause remains open.

A separate failure looked connected but was silent. Detailed logging showed initial SETUP without streams, RECORD and keepalives, but no audio-stream SETUP and no principal connection. Volume requests in that state returned 200 but logged `Principal conn is NULL`; they did not invoke the hook. PCM counters stayed static while keepalive silence advanced. Pause/resume and reconnect alone did not fix it. Routing output back to iPhone, playing locally, then selecting the bridge while playing restored audio. Walter confirmed success, and subsequent telemetry showed advancing nonzero PCM, PLAYING and no inserted silence over the sample.

## Hook timeouts (unresolved)

Repeated curl 2-second timeouts and bridge BrokenPipeError occurred during slider activity. Commands sometimes reached Cast despite the HTTP client timing out. The synchronous locked Cast calls and burst/concurrency behavior are suspects, not a verified root cause. Do not call this fixed. Current code has no request coalescing/latest-wins worker; invocation/order behavior needs testing. On the restarted session, stale /status volume_command could be mistaken for a fresh command: compare timestamps and inspect principal-session logs.

## Open work, recommended order

1. Put this package in the laptop git project, retain upstream mirror separately, restore explicit local environment values and establish safe read-only access. Do not deploy snapshots blindly. Current Hermes development stops at handoff.
2. Capture a reproducible working baseline: fresh AirPlay audio handoff, advancing PCM and nonzero blocks, independent Cast readback, session timestamps. Current mapping has a 50% cap; do not silently remove it.
3. Fix hook responsiveness: bounded asynchronous/coalesced latest-wins volume worker, acknowledge validated requests quickly, explicit desired/applied/error telemetry. Reproduce rapid slider sweeps, connection loss, mute/unmute, and ordering with real device readback. Preserve existing continuous PCM behavior.
4. Verify volume mapping while audible playback runs; compare exact protocol values and device state at several slider positions. Confirm stream samples are not double-attenuated. Test mute, zero, resume and startup target-not-ready behavior. Only then discuss lifting the cap.
5. Reproduce reconnect issues with full ordered RTSP method/path logging; investigate oversized info responses and why stream setup is missing. Do not confuse control health, stale media state or 200 responses with audio acceptance.
6. Add automated regression tests for PCM fragmentation, source stop/restart, underruns, volume bursts and process failures. Improve health checks: current health can pass with only keepalive silence and no active PCM.
7. Measure latency and buffering at each boundary. Reduce only with continuity tests; mixed native AirPlay/Cast synchronization needs a separate timestamp/clock design.
8. Pin bridge base image/dependencies and build provenance; document rollback, startup ownership and telemetry. Avoid using deploy hooks for diagnostics.

No development changes, deploy, restart, volume changes or playback commands were made during packaging. A temporary read-only Komodo container terminal was created and removed; the saved pre-deploy hook was clear before and after.
