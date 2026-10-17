# Does Shairport Sync have a canonical recommended workflow for this?

**It has documented extension points for the pieces, not a canonical end-to-end AirPlay 2 → Google Cast bridge recipe.** Our FFmpeg/HTTP/PyChromecast adapter is custom prototype code, not an upstream reference architecture or supported Cast backend.

The upstream-supported approach for this integration is to leave Shairport as the receiver and consume its published interfaces:

1. Use its documented pipe/stdout audio backends for an external audio consumer. Buffer partial reads correctly; preserve pacing and sample format.
2. Use `general.run_this_when_volume_is_set` for external device volume. The sample configuration explicitly documents the appended volume argument, -30..0 range and -144 mute sentinel.
3. Use `general.ignore_volume_control = "yes"` when the external device owns volume, to avoid software attenuation in addition to device volume. `volume_control_profile` concerns Shairport's own mixer mapping; it is not a native Cast percent mapping.
4. Run the upstream AirPlay 2 receiver with NQPTP, and follow upstream Docker guidance. Keep downstream Cast handling in a separate adapter unless a demonstrated upstream gap requires a receiver fork.
5. Use metadata/D-Bus/MPRIS/MQTT only if their documented semantics meet an additional requirement. This prototype does not require a receiver fork or a core patch just to receive volume events.

The percentage mapping, 50% test cap, HTTP POST contract, retry/order policy, Cast lifecycle, reconnect recovery and cross-protocol timing remain our responsibility. The existing hook command is documented, but the hook timeouts show our handler implementation needs work. A successful isolated curl test is not a full receiver-path test; standalone RTSP volume commands are ignored unless sent on the principal playback connection.

## Sources checked

- https://github.com/mikebrady/shairport-sync/blob/master/README.md — receiver scope, audio backends, Docker, metadata and integration overview.
- https://github.com/mikebrady/shairport-sync/blob/master/scripts/shairport-sync.conf — `general.run_this_when_volume_is_set`, `ignore_volume_control`, volume ranges and profiles.
- https://github.com/mikebrady/shairport-sync/blob/master/common.c — `command_set_volume` appends the numeric value, parses argv and execs the command (not an arbitrary shell pipeline).
- https://github.com/mikebrady/shairport-sync/blob/master/player.c — volume callback before attenuation handling.
- https://github.com/mikebrady/shairport-sync/blob/master/rtsp.c — principal-connection check for SET_PARAMETER volume.

These are upstream master references inspected during this session, not proof that every line matches the pinned published receiver image. The running image reports 5.6-dirty-AirPlay2-smi11. Pin the mirror revision on the laptop before relying on source-level equivalence.
