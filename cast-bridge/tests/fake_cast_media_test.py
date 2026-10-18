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
