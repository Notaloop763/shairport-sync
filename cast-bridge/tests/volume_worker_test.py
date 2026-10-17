"""Volume POST regression test against a fake Cast device; no networking beyond localhost."""
import sys,time,threading,types,urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bridge
class FakeCast:
    """Answers each volume call in 300 ms, like a slow Nest."""
    def __init__(self):
        self.status=types.SimpleNamespace(volume_level=0.2,volume_muted=False);self.calls=[]
    def set_volume(self,level):
        time.sleep(.3);self.calls.append(('level',level));self.status.volume_level=level
    def set_volume_muted(self,muted):
        time.sleep(.3);self.calls.append(('muted',muted));self.status.volume_muted=muted
cast=FakeCast();bridge.cast_target=cast
threading.Thread(target=bridge.volume_worker,daemon=True).start()
server=ThreadingHTTPServer(('127.0.0.1',0),bridge.Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
url=f'http://127.0.0.1:{server.server_port}/airplay-volume'
def post(value):
    start=time.monotonic()
    with urllib.request.urlopen(urllib.request.Request(url,data=str(value).encode(),method='POST'),timeout=2) as r:
        assert r.status==202
    return time.monotonic()-start
# One slider drag: eight ticks 30 ms apart. Each must answer well inside the hook's --max-time 2.
values=[-24,-23,-22,-21,-20,-19,-18,-17.25]
latencies=[]
for v in values:
    latencies.append(post(v));time.sleep(.03)
assert max(latencies)<.2,latencies
deadline=time.monotonic()+3
while bridge.stats.get('volume_applied',{}).get('airplay_db')!=values[-1]:
    assert time.monotonic()<deadline,cast.calls
    time.sleep(.05)
assert abs(cast.status.volume_level-.425)<.001 and not cast.status.volume_muted
# Superseded ticks are dropped, and the unchanged mute state costs no round-trip.
assert len(cast.calls)<len(values) and all(k=='level' for k,_ in cast.calls),cast.calls
print('PASS volume POST answers at once and applies the newest value',
      f'(max {max(latencies)*1000:.0f} ms, {len(cast.calls)} Cast calls for {len(values)} ticks)')
