# Feature request: unattended audio-behavior acceptance for the AirPlay → Cast bridge

**Status:** Requested; not implemented.
**Audience:** Agent implementing the bridge test harness and Fakecast.
**Outcome:** Replace recurring human listening tests with automated, evidence-backed checks of what audio reaches the output, when it reaches it, and how playback controls affect it.

## Problem and user intent

Fakecast was intended to remove the user from the test loop. Its present success criteria do not deliver that outcome: they establish protocol behavior and decodable audio delivery, but leave the user responsible for discovering audible latency and volume behavior.

After prototype-7 deployment, Walter confirmed sound on the bedroom device and reported:

> “Yeah I heard audio. So the audio level changes very quickly but pausing or starting music takes 7s still”

The approximate seven-second delay is user-observed, not instrumented. This feature must make that class of defect discoverable without asking the user to listen. Simulating a seven-second delay is a test fixture, not proof of the real delay's source or a fix.

**Product acceptance is automated audio behavior—not `PLAYING`, a successful LOAD, arriving bytes, or a reported volume level.**

## Verified baseline

- Repository: `https://github.com/Notaloop763/shairport-sync.git`.
- Source baseline: `95a9f051e0e1ccbef3a18caf7fad1f77c2ac9c9a`.
- Deployed image from the preceding work: `shairport-cast-bridge:prototype-7`.
- Source checkout inspected for this request: `/workspace/shairport-sync-95c7d1a4/`. HEAD is the baseline above despite the older directory name. There is an existing uncommitted `cast-bridge/compose.yaml` image-only change; preserve it.
- `cast-bridge/tools/fake_cast_device.py`, `FakeCastDevice.pull` at lines 341–361, captures HTTP bytes and declares PLAYING on receipt. It has no timed audio rendering stage.
- `listen()` decodes a captured MP3 afterward. It measures content, not wall-clock playout or command-to-output latency.
- `SET_VOLUME` updates fake device state; it does not apply gain to rendered samples.
- `cast-bridge/tests/fake_cast_media_test.py`, `play` and tone assertions at lines 40–61, inject PCM directly into the FIFO and inspect the eventual capture. This bypasses the AirPlay sender and Shairport.
- Existing reply-delay and launch-delay faults do not model independent audio buffering with prompt volume control.

Related defect report: `cast-bridge-prototype-7-playback-latency-bug.md` beside this file. Coordinate overlapping instrumentation and test changes; this request does not itself authorize implementing that report's playback fix.

## Required coverage levels

Report each level separately. A lower level must not silently substitute for a higher one.

| Level | Exercises | Establishes | Does not establish |
|---|---|---|---|
| Software regression | Scripted PCM source → real bridge/encoder/HTTP path → Fakecast decoder, clock, gain, output sink | Deterministic behavior against declared receiver models; automatic detection of known defects | Nest fidelity or AirPlay/Shairport latency |
| Full-path software integration | Automated AirPlay sender → Shairport → bridge → modeled receiver output | Behavior including negotiated AirPlay mode, receiver scheduling, and source controls | Physical Nest decoding, buffering, or acoustics |
| Hardware acceptance | Automated source/control sequence → deployed path → actual target → timestamped acoustic capture | Actual audible start/pause/resume, volume, and continuity under the recorded setup | Universal behavior across untested devices/firmware |

Software regression must run without physical hardware. Hardware acceptance must run without someone listening, pressing play, or manually scoring results once the fixture has been provisioned and the test is authorized. A stable microphone or equivalent acoustic capture device near the speaker is the proposed reference fixture. If no suitable capture/source-control interface exists, mark hardware acceptance **blocked**, identify the missing equipment or access, and leave its result non-passing. A fake output recording cannot satisfy physical-speaker acceptance.

## Functional requirements

### 1. Model playout separately from transport

Keep real Cast message exchange, HTTP pulling, and MP3 decoding. Add an explicit decoded-audio queue and playout clock feeding a deterministic recording sink. Track received, decoded, queued, and rendered samples separately.

Support receiver profiles with explicit startup buffering, steady-state queue target, underrun/rebuffer behavior, and bounded backlog. Make profile assumptions visible and versioned. Include a synthetic profile with approximately seven seconds of audio delay and prompt control handling to reproduce the reported *class* of symptom. Do not label this profile a calibrated Nest model until hardware evidence supports it.

Keep protocol response delay, app launch delay, media playout delay, and control application delay independent. Sleeping the whole receiver or delaying every reply is not an equivalent model.

### 2. Apply volume at the output stage

Apply current device gain and mute to samples at render time, including samples already queued before a volume command. Keep the encoded/input capture separate from post-gain output so analysis can distinguish a source change from device gain.

Verify output amplitude changes, mute, unmute, and latest-wins ordering from rendered samples, not only status echoes. Define the simulated gain law explicitly. Do not assume a real Nest slider fraction equals linear amplitude; calibrate its response or use measured relative changes and tolerances for hardware tests. Preserve the 0.5 command cap and test enforcement separately from the acoustic gain law.

### 3. Drive source behavior automatically

Use a scripted, identifiable signal sequence with onset/offset markers and safe amplitude. Cover initial connection/start, warm resume, pause, repeated start/stop, steady playback, rapid volume changes, mute/unmute, and selected existing recovery faults.

Distinguish source stop from silent samples, starvation, and Cast STOP/PAUSE. Exercise the bridge's existing keepalive-silence behavior. Merely forcing Fakecast to IDLE does not test an AirPlay pause.

For the full-path and hardware levels, drive a real compatible AirPlay sender programmatically and record the negotiated codec/stream mode when observable. Verify the sender/control solution actually works before treating those levels as implemented. If sender automation is unavailable, label direct-PCM tests accurately and hold full-path acceptance; do not ask the user to perform every trial as the permanent solution.

### 4. Measure behavior from output

Record action timestamps and detect output transitions from the rendered or acoustic recording. Produce separate measurements for initial start, warm resume, pause-to-silence, volume response, and mute/unmute. Detect dropouts, stale-audio replay, missing/duplicated markers, and accumulating latency over a sustained run.

Use timestamped markers or correlation rather than relying solely on the first nonzero sample. Define windowing, thresholds, noise-floor handling, and confidence/uncertainty. Identify the target signal in ambient audio; unrelated room noise must not count as successful playback. Treat absent or unusable evidence as failure or unavailable acceptance, not a pass.

Use a shared monotonic timeline when possible. Where devices have separate clocks, measure alignment and clock error or use a common recording method. Record the time of actual command dispatch separately from orchestration intent. Account for capture/analysis latency; do not infer acoustic onset from Cast status.

### 5. Produce an unattended verdict and usable artifacts

Deliver a documented executable entry point for each supported coverage level. It must run the scenario, collect evidence, analyze output, return a machine-readable verdict and meaningful process exit status, and clean up its own resources. The entry point must be implemented and exercised, not supplied as a proposed command.

A run artifact must include:

- Source revision, actual image identities, scenario, coverage level, receiver profile or hardware identity, negotiated source mode where available, and test configuration.
- Thresholds and their origin: synthetic fixture expectation, approved product budget, or calibrated hardware baseline.
- Per-trial action/output events, measured intervals, uncertainty, queue-depth/underrun evidence where observable, and aggregate ranges.
- Input markers, post-gain output recording or acoustic recording, analysis results, bounded relevant logs, and a clear reason for each failed assertion.
- Calibration provenance and stale/missing-calibration status. Report **simulation-passed**, **full-path-passed**, **hardware-verified**, **failed**, and **blocked** distinctly.

There is no agreed production latency budget yet. Establish a measured baseline and propose explicit limits for user approval before claiming latency is acceptable. Keep fixture validation thresholds distinct: detecting the deliberately injected seven-second lag should succeed as a harness test while the product-latency verdict fails whenever it exceeds the configured budget.

## Prove the oracle can fail

Include negative controls and automated assertions showing the harness detects:

1. Delayed audio start and pause while volume still affects queued output promptly.
2. Immediate fake PLAYING status while output remains silent.
3. A status acknowledgment claiming a volume change that never affects output.
4. Volume applied when audio is enqueued rather than rendered, causing gain response to lag behind backlog.
5. Wrong/stale audio, a dropout, and increasing queue latency.
6. Missing or corrupted capture, ambient noise without the target signal, and unavailable full-path prerequisites.

Compare a low-delay reference profile with the delayed profile using the same scenario. Assert expected timing differences within declared tolerances. A test suite that reports green for all injected defects is not complete.

## Implementation and coordination

Before editing, inspect checkout ownership, live writers, existing branches/open work, dependencies, and current test entry points. Coordinate overlapping changes with the playback-latency investigation. Prefer a separate worktree; preserve the incumbent Compose edit and live deployment.

Likely change anchors are `FakeCastDevice.pull`, volume handling, `listen()`, the media tests, and new scenario/oracle/fixture documentation. Keep protocol fault tests available: their coverage is useful but no longer sufficient for audio acceptance. Re-anchor against the baseline before writing; this feature request contains no implementation patch.

Implement in independently verifiable stages:

1. Software playout, independent controls, output analysis, and negative controls.
2. Verified automated AirPlay source integration and full-path scenarios.
3. Provisioned acoustic fixture, hardware measurements, model calibration, and unattended hardware acceptance.

A stage may be delivered independently, but the overall HITL-removal outcome stays **incomplete** until the full-path hardware loop is automated and proven. Calibration requires measured behavior; inserting a guessed delay into the fake does not close the goal.

## Definition of done

- A clean, documented software run generates and analyzes actual post-gain output without human participation and passes the intended reference scenarios.
- Each required negative control demonstrably produces the expected failing product verdict; harness self-tests assert those failures rather than masking them.
- Existing audio-integrity, volume/cap, and recovery regressions remain passing, with real execution evidence.
- Full-path tests operate an actual compatible AirPlay sender and Shairport rather than substituting direct FIFO input.
- Hardware acceptance drives playback and controls, records actual speaker output, and computes verdicts unattended across repeated start/pause/resume/volume trials and a sustained continuity scenario.
- Reports expose scope, identities, thresholds, uncertainty, and calibration gaps; a simulation-only pass cannot be promoted to hardware acceptance.
- The user is no longer asked “did you hear it?” as a normal release gate. One-time fixture provisioning and approval for disruptive tests are separate from recurring test execution.

## Scope and safety

This request authorizes the feature-request document only, not a live test run, implementation, deployment, or external issue publication. Obtain authority covering device interaction and service interruption before running audible or disruptive hardware scenarios. Keep `CAST_VOLUME_CAP=0.5`, use conservative test signals, and avoid autonomous repeated retries that disturb the room. Bound CPU, memory, captures, runtime, and cleanup; use loopback and test UUIDs for software runs. No broad host-network or persistent configuration changes just to make tests pass.

The current agent's `/workspace` is not guaranteed to be shared with a receiving agent. Deliver this file through the user. LAN targets, UUIDs, recordings, and deployment-specific evidence belong in private fixture configuration/artifacts, not an externally published feature request without approval. No application code or deployed state was changed while writing this document.
