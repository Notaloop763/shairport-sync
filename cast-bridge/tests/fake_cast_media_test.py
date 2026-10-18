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
