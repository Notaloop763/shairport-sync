"""Run bridge.py's main() end to end against fake_cast_device over loopback: PCM FIFO -> ffmpeg ->
/live.mp3 -> Cast LAUNCH/LOAD -> the fake pulls the stream. Runs in the bridge image (see
tests/Dockerfile) with --network host; ports 18109/18110 and 18865.
"""
import os,sys,time,json,threading,tempfile,uuid,urllib.request
from pathlib import Path
here=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(here),str(here/'tools')]
tmp=tempfile.mkdtemp();fifo=f'{tmp}/pcm'
os.environ.update(CAST_VOLUME_CAP='0.5',BRIDGE_HOST='127.0.0.1',BRIDGE_PORT='18865',
                  CAST_UUID='f4a1c0de-0000-4000-8000-0000000000a1')
import bridge
from fake_cast_device import FakeCastDevice
dev=FakeCastDevice(uuid.UUID(os.environ['CAST_UUID']),'Fake Hub','Google Nest Hub',18109,'127.0.0.1',18110)
def until(cond,timeout):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if cond(): return True
        time.sleep(.1)
    return False
feed=bridge.feed_pcm
bridge.feed_pcm=lambda proc,path:feed(proc,fifo)  # main() hard-codes /audio/pcm
threading.Thread(target=bridge.main,daemon=True).start()
assert until(lambda:bridge.stats['cast'],60),dev.log
state=json.load(urllib.request.urlopen('http://127.0.0.1:18110/state'))
assert state['app_id']=='CC1AD845',state
assert state['load']=={'contentId':'http://127.0.0.1:18865/live.mp3','contentType':'audio/mpeg','streamType':'LIVE'},state
print('PASS the fake launched',state['app_id'],'and loaded',state['load'])
assert until(lambda:len(dev.capture)>0,5) and bridge.stats['clients']==1
print('PASS the fake pulls the bridge stream:',len(dev.capture),'bytes so far')

import numpy as np
from fake_cast_device import listen
def play(pcm):
    """Write S32_LE stereo 48 kHz into the FIFO in real time, as Shairport paces it; return the
    capture slice once it has drained through ffmpeg and the stream."""
    start=len(dev.capture);fd=os.open(fifo,os.O_WRONLY);step=bridge.BLOCK;t0=time.monotonic()
    for i in range(0,len(pcm),step):
        os.write(fd,pcm[i:i+step])
        time.sleep(max(0,t0+(i+step)/(bridge.RATE*bridge.FRAME_BYTES)-time.monotonic()))
    os.close(fd);time.sleep(1.5)
    return dev.capture[start:]
def tone(hz,seconds,amp=.5):
    s=(amp*2**31*np.sin(2*np.pi*hz*np.arange(int(bridge.RATE*seconds))/bridge.RATE)).astype('<i4')
    return np.repeat(s,2).tobytes()
def assert_tone(heard,hz,seconds):
    assert heard['max_dbfs']>-40,f'silent: {heard}'
    assert heard['rate']==bridge.RATE,f'sample rate: {heard}'
    assert abs(heard['peak_hz']-hz)<=5,f'frequency: {heard}'
    assert abs(heard['seconds']-seconds)<=.1,f'duration: {heard}'
heard=listen(play(tone(1000,3)));assert_tone(heard,1000,3)
print('PASS 3 s of 1 kHz reached the fake intact:',heard)
heard=listen(play(bytes(bridge.RATE*bridge.FRAME_BYTES*2)))
try: assert_tone(heard,1000,2)
except AssertionError as exc: print('PASS silent input fails the tone check as intended: AssertionError',exc)
else: raise AssertionError(f'silent input passed the tone check: {heard}')

# Cap guard: the bridge's loudest ask stays at 0.5; a 0.6 sent past it must fail the run.
bridge.request_airplay_volume(0)
assert until(lambda:bridge.stats.get('volume_applied',{}).get('airplay_db')==0,10),bridge.stats
assert dev.level==.5,dev.level;dev.check_cap()
print('PASS AirPlay full volume reaches the fake as 0.5 and passes the cap guard')
bridge.cast_target.set_volume(.6)
try: dev.check_cap()
except AssertionError as exc: print('PASS a 0.6 SET_VOLUME fails the cap guard as intended: AssertionError',exc)
else: raise AssertionError(f'0.6 passed the cap guard: {dev.log[-3:]}')
dev.cap_violations.clear();bridge.request_airplay_volume(-21)  # back to 0.3, inside the cap
assert until(lambda:dev.level==.3,10),dev.level

# Media faults. stop_stream hits the bridge's own cast; the others need a fresh cast_audio().
T0=time.monotonic()
def say(fault,text): print(f'[{time.monotonic()-T0:5.1f}s] FAULT {fault}: {text}',flush=True)
dev.configure({'stop_stream':True})
assert until(lambda:bridge.stats['cast']['state']=='IDLE',10),(bridge.stats['cast'],dev.log[-5:])
assert until(lambda:bridge.stats['clients']==0,20),bridge.stats
say('stop_stream',f"bridge saw {bridge.stats['cast']['state']}, clients={bridge.stats['clients']}")
time.sleep(10);assert dev.player_state=='IDLE' and sum('LOAD' in l for l in dev.log)==1,dev.log
say('stop_stream','10 s later the bridge has not reloaded: the Hub stays silent until bridge.py restarts')
stream='http://127.0.0.1:18865/live.mp3'
fakes=[]
def fresh(n,**faults):
    fakes.append(FakeCastDevice(uuid.UUID(f'f4a1c0de-0000-4000-8000-0000000000b{n}'),f'Fake Hub {n}',
                          'Google Nest Hub',18109+100*n,'127.0.0.1',18110+100*n,**faults))
    return fakes[-1]
d=fresh(1,load_error=True);t=time.monotonic()
try: bridge.cast_audio('127.0.0.1',str(d.uuid),stream)
except RuntimeError as exc: say('load_error',f'cast_audio raised after {time.monotonic()-t:.0f}s: {exc!r:.80}')
else: raise AssertionError(f'cast_audio succeeded although LOAD failed: {d.log}')
assert sum('LOAD' in l for l in d.log)==1 and not d.capture,d.log
d=fresh(2,launch_delay=8);t=time.monotonic()
cast,_=bridge.cast_audio('127.0.0.1',str(d.uuid),stream)
launches=sum('LAUNCH' in l for l in d.log)
# play_media() relaunches when the app is not up yet 2 s after launch(), so two LAUNCHes.
assert launches==2 and d.state()['player_state']=='PLAYING' and d.capture,d.log
say('launch_delay',f'cast_audio succeeded after {time.monotonic()-t:.0f}s with {launches} LAUNCHes sent; {d.log[-4:]}')
for d in (dev,*fakes): d.check_cap()
print('PASS no SET_VOLUME above 0.5 reached any fake')
