"""Read-only Cast volume/media status. Requires bridge Python dependencies."""
import sys,json,time,pychromecast,zeroconf
z=zeroconf.Zeroconf(unicast=True)
cs,b=pychromecast.get_listed_chromecasts(friendly_names=[sys.argv[1]],zeroconf_instance=z,discovery_timeout=10)
try:
    c=cs[0];c.wait();c.media_controller.update_status();time.sleep(2)
    print(json.dumps({'device':c.cast_info.friendly_name,'volume':c.status.volume_level,'muted':c.status.volume_muted,'state':c.media_controller.status.player_state,'content':c.media_controller.status.content_id},indent=2))
finally:
    b.stop_discovery();z.close()
