# A2A bug report: start/pause remains ~7 seconds behind while volume is responsive

**Status:** Open — end-to-end audio confirmed by user; latency not instrumented or localized.
**Task for receiving agent:** Measure start/pause latency by pipeline stage, identify the dominant delay, and propose a bounded fix that preserves continuous audio and recovery behavior. This report supplies evidence and a diagnostic work package, not an implementation patch or deployment authorization.

## Symptom and impact

After deploying `shairport-cast-bridge:prototype-7` and switching back to the bedroom target, Walter confirmed audible audio and reported:

> “Yeah I heard audio. So the audio level changes very quickly but pausing or starting music takes 7s still”

Start/pause feels delayed despite responsive volume. Treat ~7 seconds as a user-observed estimate, not a stopwatch measurement. Trial count, source app/device, and separate start versus pause timings were not captured. Earlier project notes also record roughly seven-second latency; this is a persistent limitation, not an established prototype-7 regression.

## Baseline and target

| Item | Verified value |
|---|---|
| Repository | `https://github.com/Notaloop763/shairport-sync.git` |
| Built revision | `95a9f051e0e1ccbef3a18caf7fad1f77c2ac9c9a` |
| Build context | `cast-bridge/` |
| Running image reference | `shairport-cast-bridge:prototype-7` |
| Running image ID | `sha256:dd891938588cf7fa280abac182ef42c3b3f4323319d8fcba1ea38381b2dc60d7` |
| Komodo Stack / project | `shairport-sync` on server `Local` |
| Services / containers | `cast-bridge` / `shairport-cast-bridge`; `shairport-sync` / `shairport-sync` |
| AirPlay destination | `Nest AirPlay Bridge (Test)` |
| Cast destination | `Bedroom Display`, `192.168.77.86` |
| Cast UUID | `1af73ec2-959b-183f-f950-ad6a08e20ccc` |
| Bridge status | `http://192.168.77.89:18765/status` |
| Bridge media | `http://192.168.77.89:18765/live.mp3` |
| Volume cap | `CAST_VOLUME_CAP=0.5` — preserve |
| Shairport image | `mikebrady/shairport-sync@sha256:ff6235d83fa10c78600f4c7c803f33825bd050df3e7c3c60cc94b3572569d6d6` |

Live identity and status were read on **2026-10-18 at 04:00:10 UTC**. The bridge reported healthy with zero container restarts. These are baseline observations, not a guarantee of later state. Resolve current container identity and configuration before testing.

The stack uses Komodo-managed `config.file_contents`, not the repository Compose file, as its deployment source. Most recent intended changes were the image upgrade from prototype-6 to prototype-7, followed by a target-only change from Front door Display to Bedroom Display. Do not deploy a repository Compose snapshot over the managed configuration.

## Evidence: what is known

- User heard audio on the bedroom target after the switch.
- Status at the capture above reported `cast.state: PLAYING`, `clients: 1`, `pcm_bytes: 64994560`, and `nonzero_blocks: 8418`. Earlier status reads had smaller PCM/nonzero counters: actual input reached the bridge.
- The bedroom-switch acceptance logs contained `CAST_STARTED` for the bedroom UUID with no `RECAST_FAILURE` in the inspected startup log. Recheck current logs; that earlier check is not an ongoing guarantee.
- The latest captured volume command and applied record both specified `target_fraction: 0.4062947` and `cap: 0.5`. This is bridge-side application telemetry, not an acoustic timing measurement or independent device readback.
- `PLAYING` alone does not establish audible music: this implementation also streams keepalive silence.

## Mechanism and uncertainty

**Code-confirmed:** Volume and audio take different paths.

- Volume: Shairport hook → HTTP POST `/airplay-volume` → asynchronous latest-wins worker → Cast volume/mute control.
- Audio: AirPlay receiver → `/audio/pcm` FIFO → FFmpeg MP3 encoder → HTTP `/live.mp3` → Cast receiver playback.
- After a genuine input stop, the feeder begins inserting silence after 0.5 seconds to keep the stream alive. Normal source pause is not forwarded as a direct Cast media pause in this implementation.

**Hypothesis:** Queued audio across the pipeline delays audible start/stop while direct device-volume commands affect sound already playing. The dominant stage and contribution of each stage remain unknown. Do not attribute all seven seconds to Cast, FFmpeg, AirPlay, or a particular queue without measurement.

**Not established:** The source app's exact pause behavior, when Shairport stops producing nonzero PCM, receiver buffer depth, and whether initial start versus warm resume have the same delay. Fixed launch sleeps and recovery polling exist, but their presence does not explain steady-session start/pause latency by itself.

## Reproduction

Coordinate an audible test with the user; record source device, OS, app, connection mode, and the exact target.

1. Confirm the current stack still uses prototype-7, the bedroom UUID, and cap 0.5. Capture container image ID, configuration, and bounded startup logs.
2. On the source device select `Nest AirPlay Bridge (Test)` and play a track with a clear onset. Confirm the bedroom device is audible and PCM/nonzero counters advance.
3. With the Cast stream already established, timestamp a source pause and the actual audible stop. Distinguish an app UI change from an audio stop.
4. Timestamp source resume and the audible return. Measure initial session start separately from warm resume.
5. While playing, make a safe volume adjustment below the cap and record the control response separately.
6. Repeat pause/resume for at least five paired trials using the same setup. Preserve per-trial timings and the measurement method; report ranges rather than a single impression.

Do not use `cast.state: PLAYING` as the audible onset/offset timestamp. Prefer synchronized source/speaker recording or another repeatable acoustic method. Timestamp uncertainty must be explicit.

## Diagnostic sequence

1. **Measure the baseline without changing playback configuration.** Correlate the user action and audible transition with source/RTSP events where observable, FIFO nonzero-audio arrival/cessation, encoder output, HTTP delivery, and receiver status. Existing cumulative counters are insufficient to partition latency precisely.
2. **Add bounded instrumentation only where needed.** Distinguish silence from nonzero PCM and source pause from input starvation. Use monotonic intervals within each process; do not subtract unsynchronized clocks across devices. Document any instrumenting build and verify it does not itself change pacing.
3. **Localize before tuning.** Compare measured gaps stage by stage. Review encoder buffering, application queues, transport backpressure, receiver buffering, and AirPlay scheduling as candidates—not established causes. Queue capacity is not observed queue occupancy.
4. **Test one hypothesis per change.** Preserve a baseline and rollback artifact. Compare like-for-like acoustic trials and continuity, then propose the smallest effective change. If the dominant delay is outside bridge control, state that limit and the architectural tradeoff rather than promising a tuning fix.

## Source anchors at the built revision

All paths below are relative to the repository; line numbers refer to the exact revision above. Re-anchor by function name if the branch has moved.

| Path / anchor | Relevance |
|---|---|
| `cast-bridge/bridge.py:37–101`, `request_airplay_volume`, `volume_worker` | Independent asynchronous control path |
| `cast-bridge/bridge.py:118–165`, `feed_pcm` | FIFO reads, backpressure, input-stop detection, silence insertion |
| `cast-bridge/bridge.py:168–183`, `broadcast` | Encoder output delivery and slow-client disconnect |
| `cast-bridge/bridge.py:223–247`, `/live.mp3` handler | Per-client bounded queue and HTTP writes |
| `cast-bridge/bridge.py:276–290`, `load_stream` | LIVE MP3 load and startup status checks |
| `cast-bridge/bridge.py:321–325`, FFmpeg invocation | Raw S32_LE stereo input, 192k MP3, packet flushing |
| `cast-bridge/bridge.py:339–390`, recovery loop | Recast policy and user-takeover handling to preserve |
| `cast-bridge/tests/` | Existing regression tests; inspect their actual coverage before choosing commands |
| `cast-bridge/notes/findings.md` | Historical latency report and PCM/silence-padding failure; some sections describe older code, so do not treat the whole file as current behavior |

## Acceptance and guardrails

**Diagnosis complete when:** A repeatable baseline distinguishes initial start, warm resume, pause, and volume responsiveness; evidence localizes the dominant delay or clearly identifies what remains unobservable. Deliver per-trial measurements, method/uncertainty, the supported cause, and a concrete fix proposal or architectural limitation.

**Fix verified when:** A candidate has before/after acoustic measurements on the same setup showing a meaningful reduction against a stated target, with no new dropout/flutter, growing stale-audio backlog, or broken pause/resume. No numeric latency target has been agreed with the user; propose one from the baseline before claiming the bug fixed. Run relevant existing regressions and add coverage for changed behavior. Verify real-device acceptance after an authorized deployment.

Preserve:

- `CAST_VOLUME_CAP=0.5`; responsive latest-wins volume, mute/unmute, and single device-side gain application.
- Complete PCM frames and continuous audio. Do not restore opportunistic short-read zero padding or discard real samples to make latency appear lower.
- Bounded subscriber queues, connection recovery, and the policy of not immediately stealing playback back after user takeover.
- The current working stack and all unrelated Compose fields.

This request authorizes writing the bug report only. The receiving agent must check checkout ownership, live processes, and open work before implementation; use a separate worktree when appropriate. Live restarts/redeployments, disruptive playback experiments, external publication, and changes to device volume/permissions require authority covering those actions. No patch, PR, build, or deployment was produced as part of this report.

## Deployment-side evidence and delivery

Local artifacts, available only in the reporting agent's workspace:

- `/workspace/shairport-prototype-7/build-update.json` and `build-after.json`: successful build and revision proof.
- `/workspace/shairport-prototype-7/bedroom-switch/`: managed Compose before/after, deployment update, logs, status, and acceptance record from the target switch.
- `/workspace/shairport-sync-95c7d1a4/`: checkout now pinned to `95a9f051…`, despite its older directory name; its local `cast-bridge/compose.yaml` has an uncommitted prototype-7 image-only edit. Do not reset or commit it without checking ownership.

The recipient may not share `/workspace`; deliver this report through the user and obtain any needed evidence files separately. LAN addresses, UUIDs, Komodo details, and local paths are deployment-side context. Redact them from any externally published issue unless the user explicitly approves disclosure. No Hermes configuration or skill changes are required.
