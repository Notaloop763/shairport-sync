"""bridge.ENCODER must emit MP3 as PCM arrives from its first second, not hold a startup burst.
Needs ffmpeg; run in the bridge image (see tests/Dockerfile)."""
import sys,time,threading,subprocess
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bridge
enc=subprocess.Popen(bridge.ENCODER,stdin=subprocess.PIPE,stdout=subprocess.PIPE)
got=[0]
def rd():
    while d:=enc.stdout.read1(2048): got[0]+=len(d)
threading.Thread(target=rd,daemon=True).start()
t0=time.monotonic()
for n in range(100):  # 2 s of silence, paced as Shairport paces it
    time.sleep(max(0,t0+n/50-time.monotonic()));enc.stdin.write(bytes(bridge.BLOCK));enc.stdin.flush()
    if n==50: at1=got[0]/bridge.MP3_BYTES_PER_S
enc.kill()
assert at1>.7,f'only {at1:.2f} s of MP3 after 1 s of PCM: the encoder is buffering its input'
print(f'PASS {at1:.2f} s of MP3 out after 1 s of PCM in')
