"""Read-only PCM/underrun sample. Pass bridge /status URL and optional seconds."""
import sys,json,time,urllib.request
url=sys.argv[1]; seconds=float(sys.argv[2]) if len(sys.argv)>2 else 3
with urllib.request.urlopen(url,timeout=5) as r: a=json.load(r)
time.sleep(seconds)
with urllib.request.urlopen(url,timeout=5) as r: b=json.load(r)
print(json.dumps({'pcm_bytes_delta':b['pcm_bytes']-a['pcm_bytes'],'nonzero_blocks_delta':b['nonzero_blocks']-a['nonzero_blocks'],'inserted_silence_bytes_delta':b['inserted_silence_bytes']-a['inserted_silence_bytes'],'cast':b['cast'],'volume_command':b.get('volume_command')},indent=2))
