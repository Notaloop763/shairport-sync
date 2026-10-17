"""ACTIVE volume/mute test. Run only with deliberate authorization.
Usage: python hook_volume_test.py http://HOST:PORT 'Bedroom Display' --allow-volume-changes
Run from the bridge host network: POST endpoint source-IP allowlist applies.
This tests HTTP/Cast, NOT Shairport hook invocation. Leaves device 50%, unmuted.
To exercise the exact configured receiver command, execute the curl command from
compose.live.yaml inside shairport-sync, supplying an AirPlay value such as -22.5.
"""
import sys,time,json,urllib.request,pychromecast,zeroconf
assert sys.argv[3]=='--allow-volume-changes'
z=zeroconf.Zeroconf(unicast=True)
cs,b=pychromecast.get_listed_chromecasts(friendly_names=[sys.argv[2]],zeroconf_instance=z,discovery_timeout=10)
try:
    c=cs[0];c.wait()
    for value,expected,mute in [(-22.5,.25,False),(-15,.5,False),(0,.5,False),(-144,0,True),(-15,.5,False)]:
        req=urllib.request.Request(sys.argv[1].rstrip('/')+'/airplay-volume',data=str(value).encode(),method='POST')
        with urllib.request.urlopen(req,timeout=10) as r: print('COMMAND',json.load(r))
        time.sleep(1)
        print('CAST_READBACK',c.status.volume_level,c.status.volume_muted)
        assert abs(c.status.volume_level-expected)<.001 and c.status.volume_muted==mute
finally:
    b.stop_discovery();z.close()
