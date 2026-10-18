"""Run bridge.py's real volume path (volume_worker -> pychromecast) against fake_cast_device, one
scenario per fault switch, and print what happened. Needs PyChromecast; loopback mDNS and ports
18009/18010/18019. Slow (~4 min): it waits out pychromecast's reconnect and heartbeat timers.
Only the baseline is asserted; the fault lines are observations, recorded in notes/fake-cast-faults.md.
"""
import os,sys,time,threading,uuid
from pathlib import Path
here=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(here),str(here/'tools')]
os.environ['CAST_VOLUME_CAP']='0.5'
import bridge,pychromecast,zeroconf
from fake_cast_device import FakeCastDevice
UUID=uuid.UUID('f4a1c0de-0000-4000-8000-00000000fa4e')
dev=FakeCastDevice(UUID,'Fake Hub','Google Nest Hub',18009,'127.0.0.1',18010)
zc=zeroconf.Zeroconf(unicast=True)  # as bridge.connect() does
cs,browser=pychromecast.get_listed_chromecasts(uuids=[UUID],discovery_timeout=10,zeroconf_instance=zc)
cast=cs[0];cast.wait(timeout=10);bridge.cast_target=cast
threading.Thread(target=bridge.volume_worker,daemon=True).start()
T0=time.monotonic()
def say(fault,text): print(f'[{time.monotonic()-T0:6.1f}s] FAULT {fault}: {text}',flush=True)
def until(cond,timeout):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if cond(): return True
        time.sleep(.1)
    return False
def reset():
    dev.configure({'no_pong':False,'delay':0,'ignore_set_volume':False,'clamp_max':None,'round_step':None,
                   'set_level':1.0,'set_muted':False})
    until(lambda:cast.status and cast.status.volume_level==1.0,3)
    bridge.stats.pop('volume_error',None);bridge.stats.pop('volume_applied',None);dev.log.clear()
def send(db):
    """Queue one volume change; return (outcome, seconds) once the worker applied it or gave up."""
    bridge.stats.pop('volume_error',None);bridge.stats.pop('volume_applied',None)
    start=time.monotonic();bridge.request_airplay_volume(db)
    until(lambda:bridge.stats.get('volume_applied',{}).get('airplay_db')==db or
          bridge.stats.get('volume_error',{}).get('attempt',0)>=bridge.VOLUME_ATTEMPTS,120)
    ok=bridge.stats.get('volume_applied',{}).get('airplay_db')==db
    return ('applied' if ok else 'gave up: '+bridge.stats.get('volume_error',{}).get('error','?')),time.monotonic()-start
def show(): return f'device level={dev.level} muted={dev.muted}; bridge sees level={cast.status.volume_level} muted={cast.status.volume_muted}'
def sets(): return sum('SET_VOLUME' in l for l in dev.log)

reset();out,secs=send(-21)  # 0.3
assert out=='applied' and dev.level==.3,(out,dev.level);say('baseline',f'{out} in {secs:.1f}s; {show()}')

# 1 reset: the volume change arrives while pychromecast is reconnecting
reset();dev.configure({'drop':True});out,secs=send(-21)
say(1,f'change sent right after RST: {out} after {secs:.1f}s; {show()}')
back=until(lambda:cast.socket_client.is_connected and len([l for l in dev.log if 'accepted' in l])>0,30)
time.sleep(1);say(1,f'pychromecast reconnected={back}; after reconnect {show()}')

# 2 no PONG: pychromecast should notice and reconnect; volume changes work meanwhile
reset();dev.configure({'no_pong':True});out,secs=send(-21);say(2,f'change while PONGs stop: {out} in {secs:.1f}s')
t=time.monotonic();re=until(lambda:any('accepted' in l for l in dev.log),60)
say(2,f'pychromecast reconnected={re} after {time.monotonic()-t:.0f}s of silence')
dev.configure({'no_pong':False});until(lambda:cast.socket_client.is_connected,10)
out,secs=send(-18);say(2,f'change after reconnect: {out} in {secs:.1f}s; {show()}')

# 3 delay: 3 s is inside pychromecast's 10 s request timeout, 12 s is not
reset();dev.configure({'delay':3});out,secs=send(-21);say('3a (3 s)',f'{out} in {secs:.1f}s; {show()}')
reset();dev.configure({'delay':12});out,secs=send(-21)
say('3b (12 s)',f'{out} in {secs:.1f}s, {sets()} SET_VOLUMEs reached the device; {show()}')
time.sleep(14);say('3b (12 s)',f'14 s later: {show()}')

# 4 SET_VOLUME ignored
reset();dev.configure({'ignore_set_volume':True});out,secs=send(-21)
say(4,f'{out} in {secs:.1f}s, {sets()} SET_VOLUMEs; {show()}; /status volume_error={bridge.stats.get("volume_error")}')

# 5 the device's own cap and step
reset();dev.configure({'clamp_max':.3});out,secs=send(-18)  # asks 0.4
say('5a (cap 0.3)',f'asked 0.4: {out} in {secs:.1f}s; {show()}; stats applied target={bridge.stats["volume_applied"]["target_fraction"]}')
dev.log.clear();bridge.stats.pop('volume_applied');out,secs=send(-18)
say('5a (cap 0.3)',f'same ask again: {out}; {sets()} SET_VOLUMEs sent for a level the device already holds at its cap')
reset();dev.configure({'round_step':.05});out,secs=send(-23)  # 0.2333
say('5b (step 0.05)',f'asked 0.2333: {out}; {show()}')
dev.log.clear();bridge.stats.pop('volume_applied');out,secs=send(-23)
say('5b (step 0.05)',f'same ask again: {out}; {sets()} SET_VOLUMEs sent')

# 6 physical button: level 0.9 and muted, pushed to the sender
reset();dev.configure({'set_level':.9,'set_muted':True});time.sleep(1);say(6,f'after button press: {show()}')
out,secs=send(-21);say(6,f'bridge asks 0.3 unmuted: {out}; {show()}; log={[l[9:] for l in dev.log if "SET" in l]}')

# 7 mDNS: withdraw, then move the device to another port
reset();dev.configure({'withdraw':True});out,secs=send(-21)
say('7a withdraw',f'open connection unaffected: {out}; {show()}')
dev.configure({'advertise':{'port':18009}});reset()
def moved(port,host='127.0.0.1'):
    """Move the device (old listener closes) and reset the connection; the change arrives mid-reconnect."""
    dev.log.clear();dev.configure({'advertise':{'host':host,'port':port},'drop':True})
    out,secs=send(-21)
    came=until(lambda:any('accepted' in l for l in dev.log) and cast.socket_client.is_connected,240)
    time.sleep(1)
    return f'change sent during the move: {out} after {secs:.1f}s; pychromecast back={came} at {cast.socket_client.host}:{cast.socket_client.port}; {show()}'
say('7b new port',moved(18019))
say('7b new port',f'a change after the move: {send(-18)}; {show()}')
reset();say('7c new IP',moved(18019,'127.0.0.2')+f'; device saw {dev.log[-3:]}')
say('7c new IP',f'a change after the move: {send(-18)}; {show()}')
dev.check_cap();print('PASS no SET_VOLUME above 0.5 reached the fake')
