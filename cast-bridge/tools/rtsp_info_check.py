"""Read-only RTSP OPTIONS and GET /info probes; not full AirPlay acceptance."""
import socket,sys
host=sys.argv[1];eol=bytes([13,10])
for method,path in [('OPTIONS','*'),('GET','/info')]:
    with socket.create_connection((host,7000),timeout=5) as s:
        s.sendall(f'{method} {path} RTSP/1.0'.encode()+eol+b'CSeq: 1'+eol+eol)
        print(method,repr(s.recv(4096)))
