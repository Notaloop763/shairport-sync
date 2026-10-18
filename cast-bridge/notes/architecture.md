# Bridge architecture and direction

Read this before adding a feature to `bridge.py`. Walter's direction (2026-10-18): build the
bridge as an **adapter**, translating the AirPlay 2 dialect into the Cast dialect and back.
Reference projects are cloned in `~/Projects/ref/`; paths below are relative to it.

## The shape

Three parts, kept apart so each can change alone:

1. **AirPlay side: events in, commands out.** Shairport stays the receiver. Its metadata pipe
   delivers events; MQTT carries our commands back to the iPhone.
2. **The translation table**: one `handle(event)` that maps each event to Cast verbs. This is
   the only place a mapping rule lives.
3. **Cast side: a few verbs.** Start, pause, resume, set volume, set now-playing, plus Cast
   status changes the Hub originated. Today they're implemented with pychromecast and the
   Default Media Receiver over HTTP; Cast Streaming may replace them later (see "Delivery").

Keep it to a function and a small set of events, the way PlayOnAir does it
(`play-on-air/crates/play-on-air/src/airplay/mod.rs:47` for the events,
`cast/mod.rs:253` for the Cast trait). A class hierarchy isn't needed.

`bridge.py` today has the volume rule only (`map_airplay_volume`, fed by the curl hook) and
reads nothing else from Shairport, so it can't see pause, flush or track changes. On pause it
sends silence and the Hub plays out its buffer, so pause lags by about `lag_s` + 0.8 s on the real Hub.

## Translation table

Shairport metadata codes are `ssnc` items on the metadata pipe. Line numbers are from this
repo at `upstream/development` 380e6b1b.

| AirPlay event (code, source) | Cast action | Status |
|---|---|---|
| Pause, rate 0 (`paus`, `rtsp.c:1577`) | Cast PAUSE immediately | step 1 |
| Resume, rate 1 (`pres`, `rtsp.c:1562`) | PLAY; re-LOAD instead after a flush or a pause over 30 s | step 1 |
| Flush-all (**no event**, see below) | Cast PAUSE; mark the next resume as needing a re-LOAD | step 1, needs the receiver patch |
| Session begin/end (`pbeg`/`pend`, `player.c:5513`/`5558`) | LOAD / let the recovery loop own it | step 1 |
| Volume (`pvol`, `player.c:5193`; today the curl hook) | SET_VOLUME through the existing worker, cap honoured | done (hook) |
| Metadata block (`mdst`…`mden`, `rtsp.c:3367`; `PICT` artwork) | update Cast metadata without a re-LOAD | step 2 |

**AirPlay 2 flushes are invisible on the pipe.** `pfls` comes only from `player_flush`
(`player.c:5456`), which only classic `FLUSH` and `RECORD` call (`rtsp.c:2395`, `1019`). AP2
`FLUSHBUFFERED` (`rtsp.c:1430-1450`) sets flags and stops playback with no metadata. `prsm`
is deprecated and AP2 buffered audio uses `paus`/`pres` (`metadata/core.c:88-94`). Without a
flush event, step 1 must either re-LOAD on every resume (costs startup lag each time) or PLAY
on every resume (after a skip the Hub plays out the old track's buffered tail). Walter chose
a receiver patch instead (2026-10-18): `receiver/ap2-flush-metadata` sends `pfls` from the
AP2 flush-all path. Tracked in `tech-debt.md`.

| Cast event (Hub touch, voice, Home app) | AirPlay command (MQTT `<topic>/remote`) | Status |
|---|---|---|
| PAUSE / PLAY we didn't send | `pause` / `play` | step 3 |
| Volume we didn't set | AirPlay volume | step 3 |
| next / previous | `nextitem` / `previtem` | step 4 |

The rules for PAUSE, re-LOAD and echo suppression come from PlayOnAir:
`bridge/playback.rs:1-30` (why a resume after a flush re-LOADs: the Hub still holds pre-flush
audio) and `bridge/remote.rs:1-12` (pause locally before forwarding a Hub pause, so the
iPhone's resulting rate-0 isn't echoed back to Cast).

## Steps

Each is its own `fork/<topic>` branch.

1. **`fork/airplay-events`: metadata pipe → events → Cast PAUSE / PLAY / re-LOAD.** Add a
   `metadata` block with `pipe_name = "/audio/metadata"` to the compose config, read the pipe
   in a thread like `feed_pcm`, and add the first rows of the table. Reference parser:
   `music-assistant-server/music_assistant/providers/airplay_receiver/metadata.py` (Python,
   same pipe). Done when, on the fake Cast device, a pause produces one Cast PAUSE and a
   resume after a flush produces a re-LOAD (tests in `tests/`), and on the real Hub the pause
   lag Walter hears drops from about `lag_s` to under a second.
2. **`fork/now-playing`**: title, artist, album and artwork to the Hub's screen. Serve
   artwork from our HTTP server; update metadata in place (PlayOnAir sends a `QUEUE_UPDATE`,
   `bridge/remote.rs`). Done when a track change on the iPhone changes the Hub's screen with
   no audio gap.
3. **Reverse control: Hub play/pause/volume → iPhone over MQTT.** Needs a broker and
   `mqtt = { enabled = "yes"; enable_remote = "yes"; ... }` (off by default,
   `scripts/shairport-sync.conf:383`). Listen with
   `media_controller.register_status_listener` (`pychromecast/pychromecast/controllers/media.py:597`)
   and forward only changes we didn't command. Walter picks the broker and confirms MQTT
   before this starts. Done when a Hub touch pause pauses the iPhone without an echo loop.
4. **Next/previous**: needs a custom receiver advertising `QUEUE_NEXT|QUEUE_PREV`
   (`cast-receiver/js/receiver.js:157-199`), a $5 Cast developer registration, the Hub's
   serial registered for testing, and the receiver served over HTTP on the LAN while
   unpublished. Last, and only if Walter wants it after step 3.

## Delivery: HTTP pull vs Cast Streaming

Startup latency is a delivery problem, separate from the table. Two ways to deliver audio:

- **HTTP pull** (today): the Default Media Receiver fetches `/live.mp3`. It has a media
  session, so pause, now-playing and Hub buttons work. Latency ≈ 2 s AirPlay floor + Hub
  buffer. PlayOnAir's mitigation (`play-on-air/.../media/http.rs:28-61`): its receiver
  releases audio 8 s *before* its presentation time and bursts it on GET, so the Cast buffer
  fills with future audio and pause is left to Cast PAUSE. Our prototype-9 change only
  removed FFmpeg's 5 s input hold; it didn't release audio early, so it doesn't test this.
  Shairport's analog is a negative `audio_backend_latency_offset_in_seconds`; how far the
  pipe backend allows it is unverified. PlayOnAir measured, on Nest Audio and Mini, that
  Cast starts once ~2-4 s of audio is buffered; our Hub holds 3.4-5.6 s (`lag_s`), which fits.
- **Cast Streaming** (RTP push): audio-only app `85CDB22F`, sender-chosen `targetDelay`
  (example 400 ms), receivers buffer ≤ 1200 ms by default
  (`openscreen/cast/protocol/streaming_session_protocol.md:158,429,506`). OwnTone uses it
  and falls back to `0F5096E8` (`owntone-server/src/outputs/cast.c:79-81,1507`). Probably no
  media session, so no now-playing or Hub buttons. Unverified on a Nest Hub.

Write the Cast verbs so a second delivery can sit behind them. Before investing in Cast
Streaming, launch `85CDB22F` on the Hub with `start_app` (`pychromecast/__init__.py:442`)
and have Walter look at the screen. That takes over the Hub in his bedroom, so run it only
when he says.

## Facts established (2026-10-18)

- Our pinned image `sha256:ff6235d8…` is Docker Hub `5.6` / `latest` (2026-10-14). 5.6 is
  the first release with AirPlay 2 remote control: DACP first, then the AP2 event channel
  (`POST /command`, MediaRemote codes in `remote/remote.h`; upstream PR #2256, tested on
  iPhone hardware). DACP alone fails on iOS 17.5+ (upstream #1858).
- 5.6 still reports `RemoteControl.Available = false` under AP2 even though it works; gate
  nothing on it.
- The official image is built with the metadata pipe, MQTT, D-Bus and MPRIS
  (`docker/Dockerfile:102-105`). MQTT is the cross-container option; D-Bus works only with a
  shared host bus socket.
- Google publishes no protocol spec for the media namespace. For its JSON, read pychromecast
  (`pychromecast/controllers/media.py`) first, then `rust-cast` and `node-castv2-client`.
  `openscreen/cast/common/channel/proto/cast_channel.proto` is authoritative for framing,
  chunking and auth.

## PlayOnAir's spec, checked (2026-10-18)

`play-on-air/docs/ap2-receiver-spec.md` comes from Ghidra on Apple's frameworks plus live
receivers. Checked against Shairport (independently reverse-engineered, hardware-tested):

| Claim (spec section) | Independent check | Verdict |
|---|---|---|
| Rate 0 keeps buffered audio (§1) | Shairport clears the anchor and stops, no flush (`rtsp.c:1565-1577`) | agrees |
| Flush-all stops playback; range flush keeps playing (§2) | Same split on `flushFromSeq` (`rtsp.c:1430-1470`) | agrees |
| `POST /command` `sendMediaRemoteCommand` shape (§5) | Same keys (`remote/remote.c:216-218`) | agrees |
| `dvlc` volume report: `volume` top level and in `params` (§5) | Identical (`remote/remote.c:342-347`) | agrees |
| MediaRemote numbers (§5, listed as open) | play 0 … next 4, prev 5 (`remote/remote.h:13-25`), live-tested on iPhone (upstream #2256) | Shairport fills the gap |
| Slider linear in dB, `-30..0`, `-144` mute (§6) | Shairport's own docs (`metadata/core.c` `pvol`); our iPhone readings fit | agrees |
| Receiver pauses locally before forwarding a pause (§1, §5) | PlayOnAir tests it (`bridge/remote.rs:182`); not checkable here | plausible |
| "What the fork does" notes | Volume note is stale: PlayOnAir now uses `(dB + 30) / 30` (`airplay/mod.rs:955`) | stale |

Unchecked, because nothing in the plan depends on them yet. When a question touches one of
these, start at the section listed here:

| Topic | Where (`~/Projects/ref/play-on-air/`) |
|---|---|
| Buffered playout timing: SETRATEANCHORTIME, anchors, buffer limits | `docs/ap2-receiver-spec.md` §1, line 42 |
| PTP clock: peers, master election, lock | `docs/ap2-receiver-spec.md` §3, line 287 |
| Advertising: feature mask, `statusFlags`, `/info` keys, TXT records, an audio-only mask | `docs/ap2-receiver-spec.md` §4, line 358 |
| Cast multi-room (dynamic groups): capability bits, `MULTIZONE_STATUS`, `SET_PLAYBACK_DEVICES`, per-member volume, LIVE DMR sessions | `docs/cast-multizone-spec.md` (Messages line 120, Operations line 196); code in `crates/play-on-air/src/cast/multizone.rs`, `bridge/group.rs` |
| Why Apple multi-room can't include a Cast speaker | `VISION.md:35`, `README.md:95` |

The clock question is settled for us: Cast takes no Apple PTP timing, so the bridge never
syncs clocks with the iPhone or HomePods; the release lead under "Delivery" replaces it.

## Open questions

- Does an **unpublished** custom receiver draw next/previous and take voice "next" on a Nest
  Hub 2nd gen? Music Assistant verified it only with a published receiver. Settled by the
  step 4 test.
- What the Hub shows while running `85CDB22F`, and whether it runs it at all.
- Whether `paus` reaches the pipe fast enough to make step 1's pause instant: measure it in
  step 1.

## Reference index (`~/Projects/ref/`)

| Repo | Read it for |
|---|---|
| `play-on-air` | The whole adapter in Rust; `docs/ap2-receiver-spec.md` (AirPlay 2 messages), `docs/cast-multizone-spec.md` |
| `music-assistant-server` | `providers/airplay_receiver/` (Shairport metadata pipe in Python), `providers/chromecast/` |
| `cast-receiver` | Music Assistant's custom receiver: next/previous, custom namespace |
| `ripe-receiver` | Another AirPlay → Cast bridge's receiver page; now-playing over a custom namespace |
| `openscreen` | Google's Cast implementation: channel proto, Cast Streaming protocol |
| `owntone-server` | Cast Streaming sender in C (`src/outputs/cast.c`) |
| `pychromecast` | Our Cast library |
| `rust-cast`, `node-castv2`, `node-castv2-client` | Community docs of the media namespace JSON |
| `AirConnect` | AirPlay 1 → Cast; `aircast/src/castcore.c` for volume and LOAD handling |
