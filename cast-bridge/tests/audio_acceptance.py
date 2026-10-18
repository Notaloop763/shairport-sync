"""Unattended audio acceptance: judge what the listener would hear from rendered output, not from
PLAYING, a LOAD, arriving bytes or a volume echo. See notes/audio-acceptance.md.

  --level software    scripted marker tones -> FIFO -> the real bridge.main() (ffmpeg, /live.mp3,
                      Cast LAUNCH/LOAD, volume_worker) -> fake_cast_device with a playout profile ->
                      post-gain `output` -> marker detection. One process, one monotonic clock.
  --level full-path   blocked: no automated AirPlay sender exists yet.
  --level hardware    blocked: no microphone fixture or automated source near the Hub.
  --reanalyze DIR     re-judge a saved run from DIR/report.json and DIR/output.wav.

Writes report.json, output.wav (post-gain) and capture.mp3 (what the fake pulled) to --out. Exit 0
simulation-passed, 1 failed, 2 blocked. Runs in the test image with --network host (see
tests/Dockerfile); loopback ports 18409/18410 and 18965. ~90 s with delayed-7s.
  docker run --rm --network host -v "$PWD/cast-bridge:/src:ro" -v "$PWD/out:/out" \\
    -e SOURCE_REVISION=$(git rev-parse HEAD) -e BRIDGE_IMAGE=$(docker image inspect -f '{{.Id}}' cast-bridge:test) \\
    --entrypoint python cast-bridge:test /src/tests/audio_acceptance.py --profile delayed-7s --out /out/run1
"""
import argparse, json, os, sys, tempfile, threading, time, uuid, wave
from pathlib import Path
import numpy as np

here = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(here), str(here / 'tools')]
RATE = 48000
AMP = .25  # tone peak, -12 dBFS before device gain: loud enough to measure, safe at cap 0.5
MARKERS = [round(440 * 1.12 ** k) for k in range(16)]  # 12 % apart, so a 3 % match is unambiguous
WIN, HOP = 1920, 480  # 40 ms frames every 10 ms
FLOOR_DB, PURITY, MATCH = -50, .6, .03
UNCERTAINTY = WIN / 2 / RATE + .02  # half a frame plus one playout tick, before tick lateness
# Integrity limits hold for any receiver: the harness's own expectations of the synthetic fixture.
INTEGRITY = {'max_gap_s': .06, 'duration_error_s': .15, 'level_error_db': 1.5, 'mute_below_dbfs': -60,
             'latency_growth_s': .15, 'origin': 'synthetic fixture expectation',
             # The encoder holds ~70 ms until the 0.5 s keepalive: a short last run after a pause is
             # reported as `tail`, not a dropout (tech-debt.md item 13).
             'held_tail': {'max_s': .25, 'max_gap_s': .6}}
# shortcut: no latency budget is agreed yet; these are proposals for Walter, from the reference baseline.
BUDGET = {'start_s': 1.0, 'pause_s': 1.0, 'volume_s': .5,
          'origin': 'proposed by Claude from the reference-profile baseline; not approved by Walter'}


def scenario():
    """(kind, marker or AirPlay dB asks, seconds). 'tone' writes marker k; 'stop' writes nothing (the
    source paused; the bridge sends keepalive silence after 0.5 s); 'zeros' writes digital silence;
    'volume' sends its asks back to back through request_airplay_volume, as Shairport's hook does."""
    s = [('tone', 0, 3), ('stop', None, 4), ('tone', 1, 3), ('stop', None, 4),
         ('tone', 2, 3), ('zeros', None, 2), ('stop', None, 3), ('tone', 3, 9)]
    # Marker 3 runs 20 s so even 7 s late it is playing when each volume change lands.
    for asks in ([-27], [-144], [-15], [-24, -21, -27], [0]):  # 0.1, mute, 0.5, latest wins 0.1, cap 0.5
        s += [('volume', asks, 0), ('tone', 3, 2)]
    s += [('tone', 3, 1), ('stop', None, 2)]
    s += [('tone', k, 1.5) for k in range(4, 16)] + [('stop', None, 3)]  # sustained continuity
    return s


def drive(fifo, segments, volume, hooks):
    """Write the scenario into the FIFO in real time; return what was dispatched and when."""
    fd, events, n, phase = os.open(fifo, os.O_WRONLY), [], 0, {}
    step = RATE // 50
    t0 = time.monotonic()
    for kind, arg, secs in segments:
        if kind == 'volume':
            t = time.monotonic()
            for db in arg:
                volume(db)
            events.append({'kind': kind, 'asks': arg, 'dispatched': t})
            continue
        if kind == 'tone' and arg in hooks:
            hooks.pop(arg)()
        ev = {'kind': kind, 'marker': arg, 'seconds': secs, 'first_write': None, 'last_write': None}
        for _ in range(round(secs * 50)):
            time.sleep(max(0, t0 + n / RATE - time.monotonic()))
            if kind != 'stop':
                i = phase.get(arg, 0) + np.arange(step)
                x = AMP * np.sin(2 * np.pi * MARKERS[arg] * i / RATE) if kind == 'tone' else np.zeros(step)
                phase[arg] = i[-1] + 1
                os.write(fd, np.repeat((x * 2 ** 31).astype('<i4'), 2).tobytes())
                ev['first_write'] = ev['first_write'] or time.monotonic()
                ev['last_write'] = time.monotonic()
            n += step
        events.append(ev)
    os.close(fd)
    return events


def frames(y):
    """Per 40 ms frame every 10 ms: centre offset (s), level (dBFS), dominant frequency, and the
    share of energy around it (a pure tone is near 1, noise near 0)."""
    if len(y) < WIN:
        return np.zeros(0), np.zeros(0), np.zeros(0), np.zeros(0)
    v = np.lib.stride_tricks.sliding_window_view(y, WIN)[::HOP]
    rms, hz, pur = [], [], []
    for c in range(0, len(v), 500):
        f = v[c:c + 500]
        m = np.abs(np.fft.rfft(f * np.hanning(WIN), 8192)) ** 2
        k = m.argmax(1)
        band = np.array([r[max(i - 10, 0):i + 11].sum() for r, i in zip(m, k)])
        rms.append(np.sqrt((f ** 2).mean(1)))
        hz.append(k * RATE / 8192)
        pur.append(band / np.maximum(m.sum(1), 1e-20))
    t = (np.arange(len(v)) * HOP + WIN / 2) / RATE
    return t, 20 * np.log10(np.maximum(np.concatenate(rms), 1e-10)), np.concatenate(hz), np.concatenate(pur)


def analyze(y, t0, events, late_max=0.0):
    """Judge rendered output `y` (floats, starting at monotonic t0) against the dispatched events.
    Returns (measurements, integrity failures, budget failures)."""
    from bridge import map_airplay_volume
    tones = [e for e in events if e['kind'] != 'volume']
    span = max(e['last_write'] or 0 for e in tones) - min(e['first_write'] or 1e18 for e in tones)
    if len(y) / RATE < span:
        return {}, [f'output recording missing or shorter than the scenario ({len(y) / RATE:.1f} s < {span:.1f} s)'], []
    t, db, hz, pur = frames(y)
    t = t + t0
    near = np.abs(hz[:, None] / np.array(MARKERS)[None] - 1)
    label = np.where((db > FLOOR_DB) & (pur > PURITY) & (near.min(1) < MATCH), near.argmin(1), -1)
    runs = {}  # marker -> [[start, end]] of frames where it dominates, gaps up to 30 ms merged
    for i in np.flatnonzero(label >= 0):
        r = runs.setdefault(int(label[i]), [])
        if r and t[i] - r[-1][1] <= .035:
            r[-1][1] = t[i]
        else:
            r.append([t[i], t[i]])
    bad, over, m = [], [], {'markers': [], 'volume': [], 'uncertainty_s': round(UNCERTAINTY + late_max, 3)}
    order = [e['marker'] for e in tones if e['kind'] == 'tone']
    for k in dict.fromkeys(order):
        segs = [i for i, e in enumerate(tones) if e['marker'] == k]
        first, last = tones[segs[0]], tones[segs[-1]]
        before = tones[segs[0] - 1]['kind'] if segs[0] else None
        after = tones[segs[-1] + 1]['kind'] if segs[-1] + 1 < len(tones) else None
        start_kind = {None: 'initial_start', 'stop': 'warm_resume'}.get(before, 'transition')
        end_kind = {'stop': 'pause', 'zeros': 'silent_samples'}.get(after, 'transition')
        rec = {'marker': k, 'hz': MARKERS[k], 'start_kind': start_kind, 'end_kind': end_kind,
               'written_s': sum(tones[i]['seconds'] for i in segs), 'intervals': runs.get(k, [])}
        m['markers'].append(rec)
        if not runs.get(k):
            bad.append(f'marker {k} ({MARKERS[k]} Hz) never reached the output')
            continue
        r = runs[k]
        t_max, g_max = INTEGRITY['held_tail']['max_s'], INTEGRITY['held_tail']['max_gap_s']
        if end_kind == 'pause' and len(r) > 1 and r[-1][1] - r[-1][0] <= t_max and r[-1][0] - r[-2][1] <= g_max:
            tail = r.pop()
            rec['tail'] = {'gap_s': round(tail[0] - r[-1][1], 3), 'length_s': round(tail[1] - tail[0] + HOP / RATE, 3)}
        rec['start_latency_s'] = round(r[0][0] - first['first_write'], 3)
        rec['end_latency_s'] = round(r[-1][1] - last['last_write'], 3)
        rec['output_s'] = round(r[-1][1] - r[0][0] + HOP / RATE + rec.get('tail', {}).get('length_s', 0), 3)  # audio heard, not the held gap
        others = [s for j, rr in runs.items() if j != k for s, _ in rr]
        for (_, e1), (s2, _) in zip(r, r[1:]):
            if any(e1 < s < s2 for s in others):
                bad.append(f'marker {k} came back at {s2 - t0:.2f} s after another marker played: stale or replayed audio')
            elif k != 3 and s2 - e1 > INTEGRITY['max_gap_s']:  # marker 3 carries the mute
                bad.append(f'dropout: marker {k} silent for {s2 - e1:.2f} s at {e1 - t0:.2f} s')
        if abs(rec['output_s'] - rec['written_s']) > INTEGRITY['duration_error_s']:
            bad.append(f'marker {k} played {rec["output_s"]} s, written {rec["written_s"]} s')
        limit = BUDGET['start_s'] if start_kind != 'transition' else None
        if limit and rec['start_latency_s'] > limit:
            over.append(f'{start_kind} of marker {k} took {rec["start_latency_s"]} s (budget {limit} s)')
        if end_kind != 'transition' and rec['end_latency_s'] > BUDGET['pause_s']:
            over.append(f'{end_kind} after marker {k} took {rec["end_latency_s"]} s to fall silent (budget {BUDGET["pause_s"]} s)')
    sustained = [x['start_latency_s'] for x in m['markers'] if x['marker'] >= 5 and 'start_latency_s' in x]
    if len(sustained) > 1:
        m['latency_growth_s'] = round(sustained[-1] - sustained[0], 3)
        if abs(m['latency_growth_s']) > INTEGRITY['latency_growth_s']:
            bad.append(f'latency changed {m["latency_growth_s"]} s across the sustained run')
    for e in (e for e in events if e['kind'] == 'volume'):
        d = e['dispatched']
        level, muted, _ = map_airplay_volume(e['asks'][-1], .5)
        expect = -200.0 if muted else 20 * np.log10(AMP * level / np.sqrt(2))
        pre = db[(t >= d - .9) & (t <= d - .1)]
        post = db[(t >= d + 1.0) & (t <= d + 1.8)]
        rec = {'asks': e['asks'], 'expected': 'muted' if muted else level,
               'expected_dbfs': None if muted else round(expect, 1)}
        m['volume'].append(rec)
        if not len(pre) or not len(post):
            bad.append(f'volume {e["asks"]}: no output around it to measure')
            continue
        rec['before_dbfs'], rec['after_dbfs'] = round(float(np.median(pre)), 1), round(float(np.median(post)), 1)
        ok = rec['after_dbfs'] < INTEGRITY['mute_below_dbfs'] if muted else \
            abs(rec['after_dbfs'] - expect) <= INTEGRITY['level_error_db']
        mid = (rec['before_dbfs'] + max(expect, -70)) / 2
        win = (t >= d) & (t <= d + 1.8)
        cross = np.flatnonzero(db[win] < mid if expect < rec['before_dbfs'] else db[win] > mid)
        rec['response_s'] = round(t[win][cross[0]] - d, 3) if len(cross) else None
        if not ok:
            bad.append(f'volume {e["asks"]}: output {rec["after_dbfs"]} dBFS, expected '
                       f'{"below " + str(INTEGRITY["mute_below_dbfs"]) if muted else round(expect, 1)} (simulated gain law)')
        elif rec['response_s'] is None or rec['response_s'] > BUDGET['volume_s']:
            over.append(f'volume {e["asks"]} reached the output after {rec["response_s"]} s (budget {BUDGET["volume_s"]} s)')
    for kind in ('initial_start', 'warm_resume'):
        v = [x['start_latency_s'] for x in m['markers'] if x['start_kind'] == kind and 'start_latency_s' in x]
        m[kind + '_s'] = v and [min(v), max(v)]
    for kind in ('pause', 'silent_samples'):
        v = [x['end_latency_s'] for x in m['markers'] if x['end_kind'] == kind and 'end_latency_s' in x]
        m[kind + '_s'] = v and [min(v), max(v)]
    v = [x['response_s'] for x in m['volume'] if x.get('response_s') is not None]
    m['volume_response_s'] = v and [min(v), max(v)]
    return m, bad, over


def judge(report, y):
    m, bad, over = analyze(y, report['output_t0'], report['events'], report['playout'].get('late_max_s', 0))
    bad += report.get('cap_failures', [])  # kept in the report so --reanalyze cannot drop them
    report.update(measurements=m, integrity_failures=bad, budget_failures=over,
                  verdict='failed' if bad or over else 'simulation-passed')
    return report


def save_wav(path, y):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes((y * 32768).astype('<i2').tobytes())


def load_wav(path):
    with wave.open(str(path), 'rb') as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, RATE), 'not 16-bit mono 48 kHz'
        return np.frombuffer(w.readframes(w.getnframes()), '<i2') / 32768


def run_software(args, out):
    from fake_cast_device import FakeCastDevice, PROFILES
    os.environ.update(CAST_VOLUME_CAP='0.5', BRIDGE_HOST='127.0.0.1', BRIDGE_PORT='18965',
                      CAST_UUID='f4a1c0de-0000-4000-8000-0000000000c1')
    import bridge
    faults = json.loads(args.faults)
    oneshot = {k: faults.pop(k) for k in ('dropout', 'replay') if k in faults}
    dev = FakeCastDevice(uuid.UUID(os.environ['CAST_UUID']), 'Fake Hub', 'Google Nest Hub', 18409,
                         '127.0.0.1', 18410, profile=args.profile, **faults)
    fifo = f'{tempfile.mkdtemp()}/pcm'
    feed = bridge.feed_pcm
    bridge.feed_pcm = lambda proc, path: feed(proc, fifo)  # main() hard-codes /audio/pcm
    sys.argv[1:] = []  # main() parses argv
    threading.Thread(target=bridge.main, daemon=True).start()
    end = time.monotonic() + 60
    while not bridge.stats['cast'] and time.monotonic() < end:
        time.sleep(.2)
    if not bridge.stats['cast']:
        return {'verdict': 'failed', 'integrity_failures': ['the bridge never cast to the fake'], 'log': dev.log}
    bridge.request_airplay_volume(-15)  # 0.5 before any marker plays
    time.sleep(PROFILES[args.profile]['buffer_s'] + 2)  # let the receiver fill on keepalive silence
    asks = []
    def volume(db):
        asks.append((time.monotonic(), db)); bridge.request_airplay_volume(db)
    hooks = {10: lambda: dev.configure(oneshot)} if oneshot else {}  # mid sustained run
    events = drive(fifo, scenario(), volume, hooks)
    time.sleep(PROFILES[args.profile]['buffer_s'] + 2)  # the tail is still in the receiver's queue
    with dev.lock:
        y = np.frombuffer(bytes(dev.output), '<i2') / 32768
        capture = bytes(dev.capture)
    (out / 'capture.mp3').write_bytes(capture)
    save_wav(out / 'output.wav', y)
    try:
        dev.check_cap(); cap = []
    except AssertionError as exc:
        cap = [str(exc)]
    report = {'cap_failures': cap, 'events': events, 'output_t0': dev.output_t0, 'playout': dev.playout(),
              'faults': dict(faults, **oneshot), 'cast_state_at_end': bridge.stats['cast'].get('state'),
              'device_volume_log': [(round(ts - dev.output_t0, 3), lv, mu) for ts, lv, mu in dev.gains],
              'playout_log': dev.playout_log, 'bridge_stats': {k: v for k, v in bridge.stats.items() if k != 'sent_bytes'},
              'log': dev.log[-200:]}
    return judge(report, y)


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--level', choices=('software', 'full-path', 'hardware'), default='software')
    p.add_argument('--profile', default='reference', help='fake_cast_device PROFILES key')
    p.add_argument('--faults', default='{}', help='JSON of fake playout faults; dropout and replay fire mid-run')
    p.add_argument('--out', type=Path, default=Path(tempfile.mkdtemp(prefix='acceptance-')))
    p.add_argument('--reanalyze', type=Path, metavar='DIR')
    a = p.parse_args()
    if a.reanalyze:
        report = json.loads((a.reanalyze / 'report.json').read_text())
        try:
            judge(report, load_wav(a.reanalyze / 'output.wav'))
        except Exception as exc:  # missing or corrupt evidence is a failure, never a pass
            report.update(verdict='failed', integrity_failures=[f'output.wav unusable: {exc!r}'], budget_failures=[])
        a.out = a.reanalyze
    else:
        a.out.mkdir(parents=True, exist_ok=True)
        blocked = {'full-path': 'no automated AirPlay sender to drive Shairport; direct-FIFO runs do not count',
                   'hardware': 'no microphone fixture or automated AirPlay source at the Hub'}
        report = {'verdict': 'blocked', 'reason': blocked[a.level]} if a.level in blocked else run_software(a, a.out)
        report.update(level=a.level, scenario='software-v1', profile=a.profile,
                      source_revision=os.environ.get('SOURCE_REVISION', 'unknown'),
                      image=os.environ.get('BRIDGE_IMAGE', 'unknown'),
                      markers_hz=MARKERS, integrity_limits=INTEGRITY, budget=BUDGET,
                      calibration={'status': 'uncalibrated',
                                   'note': 'profiles are synthetic; no hardware measurement backs them'})
    (a.out / 'report.json').write_text(json.dumps(report, indent=1, default=float))
    print(json.dumps({k: report.get(k) for k in ('verdict', 'reason', 'integrity_failures', 'budget_failures')}
                     | {k: report.get('measurements', {}).get(k) for k in
                        ('initial_start_s', 'warm_resume_s', 'pause_s', 'silent_samples_s', 'volume_response_s',
                         'latency_growth_s')}, indent=1), flush=True)
    print('artifacts in', a.out, flush=True)
    sys.exit({'simulation-passed': 0, 'blocked': 2}.get(report['verdict'], 1))


if __name__ == '__main__':
    main()
