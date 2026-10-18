"""AirPlay events end to end: Shairport-format items written to the metadata FIFO drive Cast PAUSE,
PLAY and LOAD on fake_cast_device. Runs bridge.main() in the bridge image (see tests/Dockerfile) with
--network host; ports 18209/18210 and 18965. Takes about 40 s.
"""
import os,sys,time,threading,tempfile,uuid
from pathlib import Path
here=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(here),str(here/'tools')]
tmp=tempfile.mkdtemp();fifo=f'{tmp}/pcm';meta=f'{tmp}/metadata'
os.environ.update(CAST_VOLUME_CAP='0.5',BRIDGE_HOST='127.0.0.1',BRIDGE_PORT='18965',
                  CAST_UUID='f4a1c0de-0000-4000-8000-0000000000c1')
import bridge
from fake_cast_device import FakeCastDevice
dev=FakeCastDevice(uuid.UUID(os.environ['CAST_UUID']),'Fake Hub','Google Nest Hub',18209,'127.0.0.1',18210)
def until(cond,timeout):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if cond(): return True
        time.sleep(.1)
    return False
feed,read=bridge.feed_pcm,bridge.read_events
bridge.feed_pcm=lambda proc,path:feed(proc,fifo)  # main() hard-codes /audio/pcm and /audio/metadata
bridge.read_events=lambda path,handle:read(meta,handle)
threading.Thread(target=bridge.main,daemon=True).start()
assert until(lambda:bridge.stats['cast'] and dev.player_state=='PLAYING',60),(bridge.stats,dev.log)
wfd=os.open(meta,os.O_WRONLY)
def item(code,type_='ssnc'):
    return f'<item><type>{type_.encode().hex()}</type><code>{code.encode().hex()}</code><length>0</length></item>\n'.encode()
def send(*codes):
    for c in codes: os.write(wfd,item(c))
def count(word): return sum(l.endswith(' '+word) for l in dev.log)
def loads(): return sum(' LOAD ' in l for l in dev.log)
def settle(): time.sleep(1.5)  # events are handled on the bridge's thread; the fake answers within ms

# Hub controls are quiet at the start: the startup LOAD is the only one, and nothing was paused.
assert (loads(),count('PAUSE'),count('PLAY'))==(1,0,0),dev.log

# Other metadata is ignored, and an item split across two writes is still read whole.
os.write(wfd,item('minm','core'));half=item('paus');os.write(wfd,half[:20]);settle()
assert count('PAUSE')==0,dev.log
os.write(wfd,half[20:]);assert until(lambda:dev.player_state=='PAUSED',5),dev.log
print('PASS only ssnc items act, and an item split across writes is read whole')

# iPhone pause is paus then pfls: one PAUSE, and the recovery loop leaves the paused Hub alone for
# longer than LOST_AFTER plus a poll (it would re-LOAD a Hub that stopped playing).
send('pfls');settle()
time.sleep(bridge.LOST_AFTER+6)
assert (count('PAUSE'),loads(),dev.player_state)==(1,1,'PAUSED'),dev.log
print('PASS paus + pfls sent one PAUSE; the Hub stayed paused past the recovery timeout')

# iPhone resume is pres then prsm: the flush means a re-LOAD, not PLAY.
send('pres','prsm');assert until(lambda:loads()==2 and dev.player_state=='PLAYING',40),dev.log
assert count('PLAY')==0,dev.log
print('PASS pres after a flush re-LOADed instead of PLAY')

# Skip while playing is pfls then pres, with no paus.
send('pfls');assert until(lambda:count('PAUSE')==2,5),dev.log
send('pres');assert until(lambda:loads()==3 and dev.player_state=='PLAYING',40),dev.log
print('PASS a skip (pfls, pres) sent PAUSE then a re-LOAD')

# A pause with no flush keeps the Hub's buffer: resume is PLAY.
send('paus');assert until(lambda:count('PAUSE')==3,5),dev.log
send('pres');assert until(lambda:count('PLAY')==1 and dev.player_state=='PLAYING',5),dev.log
assert loads()==3,dev.log
print('PASS paus then pres with no flush sent PLAY and no new LOAD')

# A re-LOAD the Hub refuses leaves it paused; the recovery loop then casts again once it accepts.
dev.configure({'load_error':True});n=loads();send('pfls');settle();send('pres')
assert until(lambda:loads()>n,10) and dev.player_state=='PAUSED',dev.log
dev.configure({'load_error':False})
assert until(lambda:dev.player_state=='PLAYING',90),(bridge.stats['cast'],dev.log[-6:])
print('PASS a refused re-LOAD was recovered by the main loop')
dev.check_cap()
