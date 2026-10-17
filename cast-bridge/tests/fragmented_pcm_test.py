"""Synthetic FIFO regression test; does not connect to live playback."""
import sys,os,time,threading,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bridge
class Sink:
    def __init__(self): self.data=bytearray()
    def write(self,d): self.data.extend(d)
    def flush(self): pass
class Proc:
    def __init__(self): self.stdin=Sink();self.stop=False
    def poll(self): return 0 if self.stop else None
with tempfile.TemporaryDirectory() as tmp:
    fifo=str(Path(tmp)/'pcm');proc=Proc()
    def reader():
        try: bridge.feed_pcm(proc,fifo)
        except RuntimeError: pass
    t=threading.Thread(target=reader,daemon=True);t.start()
    while not os.path.exists(fifo): time.sleep(.01)
    payload=bytes(range(256))*(bridge.BLOCK*12//256);fd=os.open(fifo,os.O_WRONLY)
    try:
        for i in range(0,len(payload),333):
            os.write(fd,payload[i:i+333]);time.sleep(.001)
        deadline=time.monotonic()+2
        while len(proc.stdin.data)<len(payload) and time.monotonic()<deadline: time.sleep(.01)
        assert bytes(proc.stdin.data)==payload
        assert bridge.stats['inserted_silence_bytes']==0
        print('PASS fragmented PCM preserved byte-for-byte, no inserted silence',len(payload))
    finally:
        proc.stop=True;t.join(1);os.close(fd)
