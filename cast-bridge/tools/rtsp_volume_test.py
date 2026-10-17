"""Diagnostic RTSP volume test. MAY CHANGE VOLUME in a valid playback context.
A standalone connection receives 200 but is not principal; it does NOT verify the hook.
Usage: python rtsp_volume_test.py HOST AIRPLAY_DB
"""
import socket,sys
host=sys.argv[1];value=float(sys.argv[2]);eol=bytes([13,10])
body=f'volume: {value}'.encode()+eol
headers=[f'SET_PARAMETER rtsp://{host}/stream RTSP/1.0','CSeq: 1','Content-Type: text/parameters',f'Content-Length: {len(body)}','','']
with socket.create_connection((host,7000),timeout=5) as s:
    s.sendall(eol.join(h.encode() for h in headers)+body)
    print(s.recv(4096).decode())
print('200 alone is NOT proof of hook execution; inspect /status and independent Cast readback.')
