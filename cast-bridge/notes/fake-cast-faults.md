# Fake Cast device: fault runs against bridge.py's volume path

`tests/fake_cast_faults_test.py` runs the real `volume_worker` and pychromecast 14.0.10 against
`tools/fake_cast_device.py` over loopback, 2026-10-17, on `bridge` at 95c7d1a4 (before
`fork/volume-reconnect-retry`). Volume asks use `CAST_VOLUME_CAP=0.5`. Times are from the run.

| # | Fault | What the bridge did | Verdict |
|---|-------|---------------------|---------|
| 1 | TCP reset | Attempt 1 failed (`RequestFailed`), attempt 2 applied 1.1 s after the reset. pychromecast reconnected within 1 s. | Handled (retry) |
| 2 | No PONG | Volume changes kept working. pychromecast noticed after 20 s of silence, reconnected, and the next change applied. | Handled |
| 3a | Replies 3 s late | Applied after 3.0 s. | Handled |
| 3b | Replies 12 s late (> 10 s request timeout) | Attempt 1 `RequestTimeout`; attempt 2 `RequestFailed` (connection reset, probably because 12 s late PONGs let the heartbeat expire; not confirmed); attempt 3 sent nothing, because the late reply had already updated `cast.status` to the target. Applied after 18 s, device correct. | Handled, through the status comparison in `apply_volume` |
| 4 | SET_VOLUME ignored | 3 attempts x 10 s timeout, gave up after 32 s. Error is in `/status` `volume_error` only; device unchanged. | Handled as designed (give up); a newer slider move waits behind the 10 s call |
| 5a | Device caps level at 0.3 | Asked 0.4, device kept 0.3. `volume_applied.target_fraction` still says 0.4, so `/status` cannot show the device refused. The same ask resends SET_VOLUME every time, since 0.3 differs from 0.4. | Harmless; `/status` can disagree with the device |
| 5b | Device rounds to 0.05 steps | Asked 0.2333, device holds 0.25. Resent on repeat, as in 5a. | Harmless |
| 6 | Button press (0.9, muted) | Status push updated `cast.status` in about 1 s. The next ask sent level and unmute and both landed. | Handled |
| 7a | mDNS withdrawn | The open connection was unaffected; change applied. | Handled |
| 7b | Device moves to a new port, connection reset | Change lost: gave up after 2.0 s (`NotConnected ... is connecting`). pychromecast followed the new port after about 37 s. Device stayed at its old volume. | **Bug A** |
| 7c | Device moves to a new IP, connection reset | Change lost the same way. pychromecast reached the new IP after about 155 s. Device stayed at its old volume. | **Bug A** |

## Bug A: volume change dropped while pychromecast reconnects

`volume_worker` stops after `VOLUME_ATTEMPTS` (3) tries 1 s apart, so any reconnect longer than
about 2 s loses the command and nothing re-applies it. Fix is on `fork/volume-reconnect-retry`:
failures while the socket is not connected do not count as attempts for up to
`VOLUME_RECONNECT_WAIT` (60 s). That covers 7b but not 7c (155 s).

## Media faults

`tests/fake_cast_media_test.py` runs `bridge.main()` against the fake. The gaps were found
2026-10-18 on `fork/fake-cast-media`; `fork/bridge-recovery` closes them under Walter's recovery
policy: when the Hub drops our stream, recast at once; when a person chose something else
(another app, no app, idleReason CANCELLED or INTERRUPTED, another LOAD), wait for new AirPlay
audio (input resuming after a stall, with sound in it). Each loss gets 5 attempts, spaced 5, 10,
20 and 40 s apart, then the bridge waits for new AirPlay audio. 60 s of steady play refills the
budget. `main()` declares a loss after 10 s without PLAYING or BUFFERING of our stream.

| Fault | Before | After (`fork/bridge-recovery`) | Verdict |
|-------|--------|--------------------------------|---------|
| Stream stops mid-play (`stop_stream`) | `IDLE` within 2 s, no reload: silent until restart | Recast 14 s after the stop, no AirPlay audio needed | Recovers |
| LOAD answered with LOAD_FAILED (`load_error`) | Casting raised after 22 s and `main()` exited | Each failed recast is logged (`RECAST_FAILURE`) and retried; after the last, `RECAST_GAVE_UP` and no more LOADs. New AirPlay audio recast it 8 s after play. Tested with 2 attempts, not 5 | Recovers |
| LAUNCH 8 s late (`launch_delay`) | A second LAUNCH from `play_media()`; casting after 11 s | Unchanged | Handled |
| Another sender takes the Hub (`takeover`) | `UNKNOWN`, no reload: silent until restart | `CAST_LOST ... waiting_for_audio: true`; leaves Spotify alone for 20 s, recasts 8 s after AirPlay plays | Stays put, then recovers on AirPlay |
| The app quits (`close_app`) | Same as takeover | Same as takeover, with no app | Stays put, then recovers on AirPlay |

pychromecast reports `UNKNOWN`, not `IDLE`, for takeover and close_app, so `main()` watches for
PLAYING going away. A recast that fails while AirPlay is already playing waits for the next
pause and play: audio only counts if it resumed after the bridge gave up.

## Fidelity to the real Hub

Read from the Bedroom Display (192.168.77.86) with `cast_readback.py` once, while it was casting.
The fake's RECEIVER_STATUS and mDNS TXT keys copy that: `controlType: master`,
`stepInterval` 0.02, float32 levels, no `isActiveInput`/`isStandBy`, `version: 2`, `userEq`.

Not checked: an idle Hub's `applications` list (the fake sends `[]`), the media message shapes
(MEDIA_STATUS, the app entry, LOAD_FAILED, the takeover and app-close sequences; taken from
Google's Cast docs), whether a real Hub keeps
the session when the running app is relaunched, `cd`/`rm`/`bs` TXT values (made up), and how a real Hub behaves under any of the
seven faults. Real Hub volume rounding and caps are unknown.
