# Review of the cast-bridge prototype (`69c96d82`)

Scope: every file under `cast-bridge/`, read in full, checked against the PyChromecast 14.0.10
wheel that the `Dockerfile` pins and against the shairport-sync source on `upstream/development`
at `380e6b1b` (5.7-dev-23), which `bridge` is built on. Source citations without a directory are
shairport-sync files at the repo root, at that commit. `pychromecast/…` paths are inside that
wheel. The bridge code is byte-identical to `69c96d82`, so its citations are unchanged.

The review was first written against tag `5.6`, and the live stack still runs a receiver image
built from 5.6 (`cast-bridge/compose.yaml:3`). Where receiver behaviour differs between 5.6 and
the new base, the finding or claim says so.

Local experiments (no network, no Cast device) used `bridge.py` unmodified with fake `proc` and
fake Cast objects, in the same style as `tests/fragmented_pcm_test.py`. They are not yet checked
in as tests.

## Findings

Ranked most severe first. "Walter's call" marks findings whose fix is a behaviour choice, which
Walter makes.

### 1. High: the volume POST blocks on two serial Cast round-trips under a global lock

`cast-bridge/bridge.py:39-43`, `cast-bridge/compose.yaml:71`

`apply_airplay_volume` holds `volume_lock` across `set_volume()` and `set_volume_muted()`. In
PyChromecast 14.0.10, each of these sends a message and then waits up to `REQUEST_TIMEOUT = 10.0`
s for the device's reply (`pychromecast/controllers/receiver.py:244-266`,
`pychromecast/response_handler.py:50-57`, `pychromecast/const.py:13`). So one request costs two
device round-trips, and up to 20 s if the Nest is slow. Shairport does not wait for the hook:
`wait_for_completion` defaults to no (`common.c:1375`, `shairport.c:1269-1273`), so each slider
tick forks its own curl at once and they all queue on the lock.

Failure scenario, reproduced locally with a fake device answering in 300 ms per call. Eight
volume POSTs 30 ms apart (one slider drag) give these results:

- The first 3 requests answered in 0.6, 1.2 and 1.8 s.
- Requests 4–8 hit the hook's `--max-time 2` and curl gave up.
- All 8 values were still applied afterwards, over the next 3 s, each ending in a
  `BrokenPipeError` when the handler wrote its response to the closed socket.

That matches Hermes's curl timeouts, the `BrokenPipeError`s, and "commands sometimes reached
Cast despite the HTTP client timing out". The device lags the slider by about 0.6 s per tick
already queued. `threading.Lock` does not grant waiters in FIFO order, so the value applied last
is not guaranteed to be the last one sent.

### 2. High: Cast errors escape the POST handler and can leave volume half-applied

`cast-bridge/bridge.py:147-153`, `cast-bridge/bridge.py:42-43`

`do_POST` catches only `ValueError` and `RuntimeError`. `RequestTimeout`, `RequestFailed` and
`NotConnected` all subclass `PyChromecastError(Exception)` (`pychromecast/error.py:6-54`), so
they escape.

Failure scenario, reproduced locally: `set_volume(0.5)` succeeds, then `set_volume_muted(False)`
raises. The device has its new level but its mute state was never changed. Concretely, unmuting
from -144 to -15 leaves the Nest muted at 50%. The client gets `RemoteDisconnected` with no HTTP
status, and `stats['volume_command']` keeps its old value, so `/status` shows a stale command
as if it were current.

### 3. High: nothing recovers when the Cast session ends or another app takes over the Nest

`cast-bridge/bridge.py:255-259`

After startup, the main loop only copies `player_state` into `stats`. PyChromecast reconnects the
socket indefinitely (`tries=None`, `pychromecast/socket_client.py:271`), but nothing calls
`play_media` again.

Failure scenarios:

- **The Nest reboots, or the media session times out.** The Nest stays idle until the container
  restarts, and `/health` keeps passing (finding 5), so Docker never restarts it.
- **Someone casts another app to the Nest.** The bridge stream stops, but `cast_target` still
  points at the device. Every AirPlay volume change, including `-144` mute, is then applied to
  the other app.

What recovery should do (re-cast, or step aside for the other app) is Walter's call.

### 4. Medium: every bridge start takes over the Nest

`cast-bridge/bridge.py:210-212`, `cast-bridge/compose.yaml:47`

`cast_audio` launches the Default Media Receiver and plays the bridge stream unconditionally,
whatever the Nest is doing. `restart: unless-stopped` re-runs that after every crash, and the
bridge exits on any feeder, encoder or startup failure (`bridge.py:246-247`, `117-118`,
`205-206`, `219`).

Failure scenario: the bridge crashes during the night while the Nest is playing something else
in the bedroom. The restarted container interrupts it with the bridge stream. Whether the bridge
should cast only on demand is Walter's call.

### 5. Medium: `/health` is unconditionally healthy

`cast-bridge/bridge.py:162-168`, `cast-bridge/compose.yaml:55-60`

`/health` shares the `/status` handler and always returns 200 with the stats. That includes the
moments before Cast has started (`stats['cast']` is `None`), after the Cast session is lost
(finding 3), and while only keepalive silence is flowing.

Failure scenario: any of those states runs indefinitely with the container marked healthy.
Shairport's `depends_on: service_healthy` also passes before the bridge has a Cast target, so
volume hooks in that window get a 503.

### 6. Medium: idle keepalive silence is paced by `select` timeouts and runs slow

`cast-bridge/bridge.py:82`, `cast-bridge/bridge.py:101-110`

Once idle, each loop iteration waits for a 20 ms `select` timeout, then writes one 20 ms block.
Loop overhead goes unaccounted for, so output runs below real time.

Failure scenario, measured locally on an unloaded laptop: 10 s of idle produced 47,423
frames/s against 48,000 (0.988×). That drains the Cast buffer by about 0.7 s per idle minute,
and it drains faster on a loaded NAS. A long pause therefore ends in a receiver underrun and
rebuffering, and a dropped session is never recovered (finding 3).

The 0.5 s before idle starts also emits nothing, which leaves a 0.5 s hole in the MP3 stream
every time AirPlay input stops.

### 7. Resolved upstream (receiver): `msg_write_response` copied the body before checking its size

`rtsp.c:816-837`, `rtsp.c:839-969`; fixed by `013bd91c` (#2313, merged as `9ac7a068`)

In 5.6, `pkt` was a 4096-byte stack buffer with a 1024-byte margin. Headers were bounds-checked
as they were written, but the body was `memcpy`'d first and checked afterwards (5.6
`rtsp.c:863-872`). Any response over about 3 KB was dropped with "Attempted to write overlong
RTSP packet 3", and a body larger than the space left overflowed the stack.

On the new base, headers go into a 16 KB buffer through `response_header_append`, which checks
each field's length before advancing (`rtsp.c:816-837`). The body is checked against a 1 MB
limit before anything is allocated or copied (`rtsp.c:919-924`), and the packet is then
allocated at its exact size (`rtsp.c:925-934`). Neither failure scenario applies to a response
under 1 MB.

The upstream commit also names the response that overflowed: the PTP SETUP reply lists every
non-loopback interface address (`rtsp.c:2594-2618`; the same loop is in 5.6). The receiver runs
with `network_mode: host` (`cast-bridge/compose.yaml:5`) on a NAS that hosts Docker networks, so
that reply is the likely cause of Hermes's reconnect failure. It stays a lead until the
receiver's logs or the NAS's interface list confirm it.

The live stack still runs the 5.6 image (`cast-bridge/compose.yaml:3`), so the bug is live until
a receiver built from `bridge` is deployed. When to deploy is Walter's call.

### 8. Low-Medium: the first session after a receiver restart sets the Nest to 20%

`player.c:3826-3828`, `player.c:3398-3404`, `shairport.c:569`, `shairport.c:2062`

Every player start calls `player_volume(suggested_volume(conn))`, which fires the hook. The
argument is the connection's own volume if the sender has already sent one. Otherwise it is
`config.airplay_volume`, which starts at `default_airplay_volume = -24.0`.

Failure scenario: after a receiver redeploy, the first stream sets the Nest to 0.2 before the
iPhone's real slider value arrives. This contradicts the intent at `bridge.py:209` ("Preserve
the user's current device volume"). It's safe (downward), but it's a surprise.

### 9. Low: the stream and status endpoints are open to the whole LAN

`cast-bridge/bridge.py:161-194`, `cast-bridge/bridge.py:215`

`/live.mp3`, `/status` and `/health` have no allowlist. Anyone on the LAN can listen to whatever
is playing to the bedroom, or read telemetry. Any LAN listener also satisfies the startup check
`stats['clients'] > 0`, which is meant to confirm the Nest connected.

The POST allowlist (`bridge.py:139`) admits `BRIDGE_HOST`. That is the NAS's own LAN address, so
any process or host-network container on the NAS can set the volume. Who may call which endpoint
is Walter's call.

### 10. Low: the PCM format is assumed in the bridge, not pinned in the receiver config

`cast-bridge/bridge.py:15-16`, `cast-bridge/bridge.py:238`, `cast-bridge/compose.yaml:73`

The bridge hardcodes S32_LE, 48 kHz, 2 channels. That's correct today only because the config
sets no `output_format`, `output_rate` or `output_channels`, so the pipe backend's AirPlay 2
defaults apply (`audio_pipe.c:115`, `audio.c:239-244`).

Failure scenario: someone adds `output_format = "S16"` to the config, and FFmpeg misreads the
stream as full-scale noise, with no error anywhere. Pinning the three keys in `pipe = { … }`
makes the contract explicit.

### 11. Low: the tool docs contradict `CLAUDE.md`

- `cast-bridge/tools/rtsp_volume_test.py:1` says it "MAY CHANGE VOLUME", but `CLAUDE.md` says
  every tool except `hook_volume_test.py` is read-only. Per `rtsp.c:3231` it changes nothing
  unless it lands on the principal connection, but a misread could still move the volume. This
  review treats it as volume-changing and did not run it.
- `cast-bridge/tools/hook_volume_test.py:6` refers to `compose.live.yaml`, which doesn't exist.

### 12. Low: reproducibility and diagnostics

- **Unpinned build inputs.** The base image is the `python:3.12-slim` tag, not a digest, and
  PyChromecast's dependencies are unpinned (`cast-bridge/Dockerfile:1-3`).
- **Logs rotate quickly.** The receiver runs `-vvv --statistics` with 5 MB × 3 log rotation
  (`cast-bridge/compose.yaml:31`, `38-42`), so during an incident the history needed to
  diagnose it rotates away within minutes.

## Claims

Every factual claim in `notes/findings.md` and `notes/workflow.md`. Statements that are advice
rather than claims, such as "buffer partial reads correctly", are left out.

### `notes/findings.md`

| # | Claim | Verdict | Settled by |
|---|---|---|---|
| F1 | 48 kHz stereo S32_LE PCM flows through `/audio/pcm` | confirmed | `audio_pipe.c:115` AirPlay 2 defaults; `audio.c:239-244` apply when unset; `compose.yaml:73` sets none |
| F2 | FFmpeg encodes 192 kbps MP3 | confirmed | `bridge.py:237-240` |
| F3 | An HTTP stream feeds the Nest via PyChromecast | confirmed (code) / untestable (device identity) | `bridge.py:212`; the UUID→"Bedroom Display" mapping needs the LAN |
| F4 | No synchronised mixed AirPlay/Cast playback | confirmed | no clock or timestamp handling anywhere in `bridge.py` |
| F5 | End-to-end latency is about 7 s, user-observed | untestable here | needs live playback |
| F6 | Shairport appends the AirPlay value to the curl command | confirmed | `common.c:1353` `"%s %f"`, `poptParseArgvString` then `execv` (`common.c:1358-1370`) |
| F7 | `ignore_volume_control = "yes"` disables Shairport gain | confirmed | `player.c:5315-5316` leaves attenuation at `max_db`; `player.c:5275-5277` skips mute |
| F8 | -30..0 maps via `(v+30)/30`; -144 mutes; 0 mutes; invalid input rejected | confirmed | `bridge.py:25-33`; `tests/mapping_unit_test.py` passes |
| F9 | The cap clamps rather than rescales; the device never goes above 50% | confirmed | `bridge.py:33`; `compose.yaml:51` |
| F10 | Source-IP allowlisting is not authentication | confirmed | `bridge.py:139`; see finding 9 |
| F11 | Concurrent volume requests serialise on a lock | confirmed, with a caveat | `bridge.py:39`; serialised but not ordered, and each holds the lock for two blocking calls (finding 1) |
| F12 | -20.675676 requested 31.1%; device read 31% | confirmed (arithmetic) / untestable (readback) | (−20.675676+30)/30 = 0.3108 |
| F13 | -10.684813 mapped to 64.4%, capped to 50% | confirmed (arithmetic) | 19.315187/30 = 0.6438 → min(0.6438, 0.5) |
| F14 | Pink-noise SPL table, 7 ft, about 49 dB background | untestable here | user-reported acoustic measurement |
| F15 | The old feeder used a 20 ms deadline and padded short reads | untestable here | that code is not in this repo; `69c96d82` is the first import |
| F16 | An earlier probe suggested about 15% PCM missing | untestable here | no probe code or data in the repo |
| F17 | The current feeder accumulates fragments, emits only whole blocks, retains partial bytes, and follows Shairport's pacing and backpressure | confirmed | `bridge.py:84-100`; Shairport writes with a blocking fd (`common.c:1773-1789`, `audio_pipe.c:87`) |
| F18 | After a 500 ms input stop, silence keeps Cast connected | confirmed, with a caveat | `bridge.py:101-110`; it runs at 0.988× real time (finding 6) |
| F19 | The fragmented-write test preserved 92,160 bytes exactly | confirmed | ran `tests/fragmented_pcm_test.py`: PASS, 92160 |
| F20 | Live samples after the fix showed zero inserted silence, and the sound was steady | untestable here | live |
| F21 | After receiver recreation, iPhone attempts failed with "overlong RTSP packet 3" | message confirmed in 5.6 / occurrence untestable | the message was 5.6 `rtsp.c:870`; mechanism in finding 7. On the new base it appears only above the 16 KB header or 1 MB packet limit (`rtsp.c:830`, `rtsp.c:920`) |
| F22 | A receiver-only restart let the control connection through | untestable here | live |
| F23 | `GET /info` returning 200 does not validate the full handshake | confirmed | `tools/rtsp_info_check.py` sends unauthenticated OPTIONS and GET only |
| F24 | The silent session had a SETUP without streams but no audio-stream SETUP, and so no principal connection | mechanism confirmed / observation untestable | in AirPlay 2 the principal is taken only on the SETUP with streams (`rtsp.c:2690-2705`, via `get_play_lock` at `rtsp.c:383-392`) |
| F25 | In that state, volume requests returned 200, logged "Principal conn is NULL", and did not run the hook | confirmed | `rtsp.c:3231-3242` (hook only for the principal), `rtsp.c:3419` (always 200). The message is now `debug(2)`, not `debug(1)` (`35f847d1`); the receiver's `-vvv` still logs it |
| F26 | PCM counters stayed static while keepalive silence advanced | untestable here | consistent with `bridge.py:101-110` |
| F27 | Pause/resume didn't help; routing back to the iPhone and re-selecting the bridge did | untestable here | live |
| F28 | curl 2 s timeouts and `BrokenPipeError` occurred during slider activity | confirmed (reproduced) | local burst experiment, finding 1 |
| F29 | Commands sometimes reached Cast after the client timed out | confirmed (reproduced) | same experiment: all 8 applied, 5 timed out |
| F30 | The locked synchronous Cast calls are a suspect, not a verified cause | refuted as "unverified": they are a sufficient cause | `pychromecast/controllers/receiver.py:244-266`; finding 1 |
| F31 | There is no coalescing or latest-wins worker | confirmed | `bridge.py:36-49` |
| F32 | A stale `volume_command` in `/status` can look fresh | confirmed | `bridge.py:44` only updates on success; finding 2 |
| F33 | Health can pass with only keepalive silence and no active PCM | confirmed | `bridge.py:162-168`; finding 5 |
| F34 | Nothing was changed, deployed or restarted during packaging; a read-only Komodo terminal was created and removed | untestable here | NAS/Komodo history |

### `notes/workflow.md`

| # | Claim | Verdict | Settled by |
|---|---|---|---|
| W1 | Upstream has no canonical AirPlay 2 → Cast recipe | confirmed | no "chromecast" or "google cast" anywhere in `upstream/development`; in this checkout only in `cast-bridge/` and `CLAUDE.md` |
| W2 | The pipe and stdout backends are documented extension points | confirmed | `audio_pipe.c`, `audio_stdout.c`; `scripts/shairport-sync.conf` pipe stanza |
| W3 | The sample config documents the appended argument, the -30..0 range and the -144 mute | confirmed | `scripts/shairport-sync.conf:86-88` |
| W4 | `ignore_volume_control = "yes"` avoids software attenuation | confirmed | `player.c:5315-5316` |
| W5 | `volume_control_profile` only affects Shairport's own mixer mapping | confirmed | `player.c:5316-5325` uses it only when `ignore_volume_control == 0` |
| W6 | Volume events need no receiver fork or core patch | confirmed | `shairport.c:1021` reads `general.run_this_when_volume_is_set`; finding 7 was a separate reason to patch, and upstream has fixed it |
| W7 | Standalone RTSP volume commands are ignored unless sent on the principal connection | confirmed | `rtsp.c:3231-3242` |
| W8 | `command_set_volume` appends the value, parses argv and execs directly, not via a shell | confirmed | `common.c:1341-1370` |
| W9 | `player.c` runs the volume callback before attenuation handling | confirmed | `player.c:5426-5427` |
| W10 | `rtsp.c` checks the principal connection before SET_PARAMETER volume | confirmed | `rtsp.c:3229-3231` |
| W11 | The running image reports `5.6-dirty-AirPlay2-smi11` | untestable here | needs the NAS. "dirty" means it was built from a modified tree, so the digest in `compose.yaml:3` isn't proven to equal tag 5.6; "SMI 11" matches commit `4ff74895`, which is in 5.6. A receiver built from `bridge` would report a 5.7-dev version instead |
