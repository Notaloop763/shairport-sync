"""Pure mapping test; no networking or volume changes."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge
for value, expected in [(-144,(0,True)),(-30,(0,True)),(-22.5,(.25,False)),(-15,(.5,False)),(-7.5,(.5,False)),(0,(.5,False))]:
    assert bridge.map_airplay_volume(value,.5)==expected
for value in [float('nan'),float('inf'),-31,1]:
    try: bridge.map_airplay_volume(value,.5)
    except ValueError: pass
    else: raise AssertionError(value)
print('PASS mapping, mute, cap, invalid inputs')
