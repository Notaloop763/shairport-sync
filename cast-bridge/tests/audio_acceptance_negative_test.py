"""Prove the audio-acceptance oracle can fail: run audio_acceptance.py once per injected defect and
assert it reports 'failed' for the right reason, after a clean 'reference' run that must pass.
Then damage the reference run's saved evidence and re-judge it. ~15 min; run alone (loopback ports
and mDNS are shared with the other fake-Hub tests). See notes/audio-acceptance.md "Prove the oracle can fail".
  docker run --rm --network host -v "$PWD/cast-bridge:/src:ro" --entrypoint python cast-bridge:test /src/tests/audio_acceptance_negative_test.py
"""
import json, subprocess, sys, tempfile, wave
from pathlib import Path
import numpy as np

HARNESS = Path(__file__).resolve().parent / 'audio_acceptance.py'
out = Path(tempfile.mkdtemp(prefix='negative-'))
bad = []


def run(name, *args):
    d = out / name
    p = subprocess.run([sys.executable, str(HARNESS), '--out', str(d), *args], capture_output=True, text=True)
    return p.returncode, json.loads((d / 'report.json').read_text()), d


def expect(name, ok, why):
    print(f'{"PASS" if ok else "FAIL"} {name}: {why}', flush=True)
    if not ok:
        bad.append(name)


def failure(name, report, key, text):
    hit = [f for f in report[key] if text in f]
    expect(name, report['verdict'] == 'failed' and bool(hit), f'{report["verdict"]}; {key} {hit[:1] or report[key][:2]}')


code, ref, ref_dir = run('reference', '--profile', 'reference')
expect('reference passes', code == 0 and ref['verdict'] == 'simulation-passed',
       f'exit {code}, {ref["verdict"]} {ref.get("integrity_failures")} {ref.get("budget_failures")}')

_, r, _ = run('delayed-7s', '--profile', 'delayed-7s')  # 1: late start and pause, volume prompt
failure('delayed start and pause', r, 'budget_failures', 'initial_start')
failure('delayed pause', r, 'budget_failures', 'pause after')
expect('delayed profile keeps volume prompt', not r['integrity_failures'] and r['measurements']['volume_response_s'][1] < .5,
       f'integrity {r["integrity_failures"][:1]}, volume {r["measurements"]["volume_response_s"]}')
_, r, _ = run('silent', '--faults', '{"silent_output": true}')  # 2: PLAYING while output is silent
failure('PLAYING but silent output', r, 'integrity_failures', 'never reached the output')
_, r, _ = run('ack-only', '--faults', '{"control_delay": 100000}')  # 3: volume ack that never affects output
failure('volume ack without effect', r, 'integrity_failures', 'volume')
_, r, _ = run('enqueue', '--profile', 'delayed-7s', '--faults', '{"gain_at_enqueue": true}')  # 4: gain lags the backlog
failure('gain applied at enqueue', r, 'integrity_failures', 'volume')
_, r, _ = run('dropout', '--faults', '{"dropout": 0.5}')  # 5: dropout, replay, growing queue
failure('dropout', r, 'integrity_failures', 'dropout')
_, r, _ = run('replay', '--faults', '{"replay": 2.0}')
failure('stale or replayed audio', r, 'integrity_failures', 'stale or replayed')
_, r, _ = run('slow-render', '--faults', '{"render_rate": 0.98}')
failure('growing queue latency', r, 'integrity_failures', 'latency changed')
code, r, _ = run('full-path', '--level', 'full-path')  # 6: prerequisites missing
expect('full-path blocked, not passed', code == 2 and r['verdict'] == 'blocked', f'exit {code}, {r["verdict"]}')


def reanalyze(name, damage):
    d = out / name
    d.mkdir()
    (d / 'report.json').write_text((ref_dir / 'report.json').read_text())
    damage(d)
    p = subprocess.run([sys.executable, str(HARNESS), '--reanalyze', str(d)], capture_output=True, text=True)
    return p.returncode, json.loads((d / 'report.json').read_text())


def write_wav(path, y):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype('<i2').tobytes())


def record_cap(d):
    """The saved run was clean; the report now says a SET_VOLUME above the cap reached the device."""
    (d / 'output.wav').write_bytes((ref_dir / 'output.wav').read_bytes())
    rep = json.loads((d / 'report.json').read_text())
    (d / 'report.json').write_text(json.dumps(dict(rep, cap_failures=['SET_VOLUME above cap 0.5: [0.6]'])))


with wave.open(str(ref_dir / 'output.wav')) as w:
    n = w.getnframes()
rng = np.random.default_rng(1)
for name, damage in {
        'missing capture': lambda d: None,
        'truncated capture': lambda d: write_wav(d / 'output.wav', np.zeros(48000 * 5)),
        'ambient noise, no target signal': lambda d: write_wav(d / 'output.wav', .05 * rng.standard_normal(n)),
        'cap violation recorded in the report': record_cap}.items():
    code, r = reanalyze(name.split(',')[0].replace(' ', '-'), damage)
    expect(name, code == 1 and r['verdict'] == 'failed', f'exit {code}, {r["verdict"]} {r["integrity_failures"][:1]}')

print('artifacts in', out)
sys.exit(1 if bad else 0)
