# Tech debt

What is wrong with the bridge setup today, most costly first. Each item names the evidence,
what it costs Walter, and the fix. The plan that pays most of it down is in
`architecture.md`; items point at its steps rather than repeating them.

Retire an item by deleting it in the commit that fixes it, after checking the fix on the
real Hub where the item names a real-Hub symptom. Add an item when a session finds a defect
or a shortcut and doesn't fix it in the same change.

## Behaviour Walter notices

**1. The bridge can't see pause, resume or track changes.** `bridge.py` reads only the PCM
pipe and the volume hook. On pause it sends keepalive silence and the Hub plays out what it
has buffered: pause lags ~`lag_s` + 0.8 s (4-5.5 s measured on the real Hub). Fix:
`architecture.md` step 1.

**2. AirPlay 2 flushes emit no metadata event.** Shairport sends `pfls` only for classic
FLUSH; AP2 `FLUSHBUFFERED` stops playback silently (`rtsp.c:1430-1450`). Without it the
bridge can't apply the "resume after a flush re-LOADs" rule, and the Hub would replay the old
track's buffered tail after a skip. Fix, Walter's choice: carry a receiver patch,
`receiver/ap2-flush-metadata`, that sends `pfls` from the AP2 flush-all path.
- Done 2026-10-18: the patch builds (AirPlay 2 and classic, no warnings), passed an iPhone
  test on the laptop (`architecture.md` "What an iPhone actually sends"), and is merged into
  `bridge`.
- Deployed 2026-10-18 as `shairport-cast-receiver:prototype-1` (built from `8f01b939`).
  The NAS receiver log shows one `pfls` per AirPlay 2 immediate flush during Walter's
  iPhone test (5 skips). Nothing reads it yet: the bridge picks it up in step 1.
- It is a candidate upstream PR; open one only when Walter says (`UPSTREAMING.md`).
- Retired when upstream ships an equivalent and we're back on an upstream image.

**3. Startup latency ~7 s.** Three stacked delays: the ~2 s AirPlay floor, the Hub filling
3.4-5.6 s of buffer before PLAYING (`lag_s`), and the encoder. The bridge releases audio only
at presentation time, so the Hub's start threshold is filled in real time. Untried levers:
a negative `audio_backend_latency_offset_in_seconds` (release early, PlayOnAir's technique
in Shairport terms) and Cast Streaming. See `architecture.md` "Delivery".

**4. The Hub's controls don't reach the iPhone.** A touch, voice or Home app pause or volume
change on the Hub changes only the Hub; the iPhone still shows playing and its slider drifts
from the Hub's level. Shairport 5.6 can send the commands; MQTT is off in our config. Fix:
`architecture.md` step 3, after Walter picks a broker.

**5. The Hub's screen shows a fixed "AirPlay Bridge" title.** `load_stream` hard-codes
`title='AirPlay Bridge'` and sends no artist, album or artwork. Fix: step 2.

**6. No next/previous on the Hub.** The Default Media Receiver is single-item and never
advertises `QUEUE_NEXT`/`QUEUE_PREV`. Fix: step 4 (custom receiver), only if Walter wants it.

## Design shortcuts

**7. No event seam in `bridge.py`.** Module globals (`stats`, `cast_target`,
`volume_pending`) and a `main()` loop that mixes recovery, LOAD and `lag_s` telemetry.
Every new feature would add another special case to that loop. Fix: the `handle(event)`
shape in `architecture.md`, built as step 1 lands, not as a separate rewrite.

**8. Volume arrives on its own channel.** The curl hook forks a process per slider tick
(`compose.yaml` `run_this_when_volume_is_set`), which produced 2 s curl timeouts before the
latest-wins worker. Once step 1 reads the metadata pipe, `pvol` carries the same value
there. Fix: move volume onto the pipe when step 1 is stable; keep the worker and the 0.5
cap.

**9. Lossy second encode.** AirPlay audio (often AAC, sometimes ALAC) is decoded by
Shairport and re-encoded to 192 kbps MP3 by FFmpeg. PlayOnAir sends FLAC or WAV to Cast
instead. Costs fidelity and adds encoder buffering. Fix: try FLAC or WAV over the same HTTP
path; check the Hub accepts it.

## Measurement and health

**10. `/health` passes on keepalive silence alone.** It returns `stats` whenever the
process is up, so the compose health check stays green with no AirPlay audio and no Cast
playback. Fix: report unhealthy when Cast isn't PLAYING our stream for longer than the
recovery budget.

**11. `lag_s` rests on two assumptions.** It assumes MP3 CBR at exactly 24,000 B/s
(`MP3_BYTES_PER_S`) and that the Hub holds exactly one `/live.mp3` connection. Changing the
codec (item 9) or bitrate breaks it silently. Fix: derive the byte rate from the encoder
settings, or measure position against PCM sent.

**12. The fake Cast device is not the Hub.** Its buffering, volume rounding and session
behaviour are guesses (`fake-cast-faults.md` "Fidelity to the real Hub"), and the burst
experiment showed the real Hub behaves differently. Every latency or pause claim needs a
real-Hub check with Walter listening.
