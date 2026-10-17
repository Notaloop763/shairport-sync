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
# Unmute from -144 to -15 where the mute call fails twice: the level lands, mute does not,
# so the Nest sits muted at 50%. The worker reports it and retries until both match.
bridge.VOLUME_RETRY_DELAY=.1
failures=[RuntimeError('RequestTimeout'),RuntimeError('NotConnected')]
real_muted=cast.set_volume_muted
def flaky_muted(muted):
    if failures: raise failures.pop(0)
    real_muted(muted)
cast.set_volume_muted=flaky_muted
cast.status.volume_muted=True;cast.calls.clear()
post(-15)
deadline=time.monotonic()+3
while bridge.stats.get('volume_applied',{}).get('airplay_db')!=-15:
    assert time.monotonic()<deadline,(cast.calls,bridge.stats.get('volume_error'))
    time.sleep(.05)
assert cast.status.volume_level==.5 and not cast.status.volume_muted,cast.status
assert cast.calls==[('level',.5),('muted',False)],cast.calls
assert 'volume_error' not in bridge.stats
# A device that keeps failing is given up on after VOLUME_ATTEMPTS, with the error kept for /status.
def dead(level): raise RuntimeError('NotConnected')
cast.set_volume=dead;cast.calls.clear()
post(-20)
time.sleep(bridge.VOLUME_RETRY_DELAY*bridge.VOLUME_ATTEMPTS+.3)
assert bridge.stats['volume_error']['attempt']==bridge.VOLUME_ATTEMPTS,bridge.stats['volume_error']
assert bridge.stats['volume_applied']['airplay_db']==-15
print('PASS failed volume applies are retried, reported, then given up')
# A Hub that is mid-reconnect longer than VOLUME_ATTEMPTS retries must not lose the command.
cast.socket_client=types.SimpleNamespace(is_connected=False)
def refused(level):
    if cast.socket_client.is_connected: return FakeCast.set_volume(cast,level)
    raise RuntimeError('NotConnected')
cast.set_volume=refused;cast.status.volume_level=.2;cast.calls.clear()
post(-21)
time.sleep(bridge.VOLUME_RETRY_DELAY*(bridge.VOLUME_ATTEMPTS+2))
assert 'volume_applied' in bridge.stats and bridge.stats['volume_applied']['airplay_db']==-15
cast.socket_client.is_connected=True
deadline=time.monotonic()+3
while bridge.stats['volume_applied']['airplay_db']!=-21:
    assert time.monotonic()<deadline,bridge.stats.get('volume_error')
    time.sleep(.05)
assert abs(cast.status.volume_level-.3)<.001
print('PASS a volume change waits out a reconnect longer than the retry count')
