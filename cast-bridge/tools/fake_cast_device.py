"""Fake Google Cast receiver for reproducing Nest Hub failures; sits beneath pychromecast.

Needs pychromecast (for its generated CastMessage proto) and zeroconf. Touches only localhost/LAN
sockets of its own, never the real Hub.

What bridge.py asks of a Cast device (pychromecast 14.0.10) and the messages that produces.
"fake" says whether this module answers it; the rest is out of scope for the volume path.

  discover()      get_chromecasts(timeout=15), stop_discovery   mDNS browse only          fake: advertised
  cast_audio()    Zeroconf(unicast=True)                        mDNS                       fake: advertised
                  get_listed_chromecasts(uuids=[..])            mDNS (TXT id/fn/md, SRV)   fake: advertised
                  cast.wait(timeout=20)                         TLS connect (CERT_NONE),
                                                                tp.connection CONNECT,
                                                                receiver GET_STATUS,
                                                                heartbeat PING every 10 s  fake: answered
                  media_controller.launch() / play_media()      receiver LAUNCH, media LOAD fake: NOT answered
                  media_controller.status                       media GET_STATUS           fake: NOT answered
                  cast_info.friendly_name / host / model_name   mDNS TXT                   fake: advertised
  apply_volume()  cast.status.volume_level / volume_muted       cached RECEIVER_STATUS     fake: answered
                  cast.set_volume(level)                        receiver SET_VOLUME {volume:{level}}
                  cast.set_volume_muted(muted)                  receiver SET_VOLUME {volume:{muted}}
                                                                both wait on a RECEIVER_STATUS
                                                                echoing requestId (10 s)   fake: answered
  (implicit)      pychromecast answers our PING with PONG; sends tp.connection CLOSE on stop.

Framing: 4-byte big-endian length + protobuf CastMessage. No device-auth handler: pychromecast
never sends one.

Faults, settable at startup (flags) or at runtime: POST JSON to http://127.0.0.1:<control-port>/
  {"drop": true}                 reset every open TCP connection now
  {"no_pong": true}              stop answering PING
  {"delay": 3.0}                 delay every reply by N seconds
  {"ignore_set_volume": true}    accept SET_VOLUME, change nothing, send no reply
  {"clamp_max": 0.4, "round_step": 0.05}   the device's own cap / quantisation of SET_VOLUME levels
  {"set_level": 0.7, "set_muted": true}    a physical button: change state, push status to senders
  {"withdraw": true}             drop the mDNS advertisement
  {"advertise": {"host": "127.0.0.2", "port": 8019}}   (re-)advertise, optionally elsewhere; the
                                 old listener closes and a new one opens on the new port
GET / returns the current state as JSON.
"""
import argparse
import json
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pychromecast.generated.cast_channel_pb2 import CastMessage
from zeroconf import ServiceInfo, Zeroconf

NS_CONNECTION = 'urn:x-cast:com.google.cast.tp.connection'
NS_HEARTBEAT = 'urn:x-cast:com.google.cast.tp.heartbeat'
NS_RECEIVER = 'urn:x-cast:com.google.cast.receiver'


def f32(x):
    return struct.unpack('f', struct.pack('f', x))[0]


USER_EQ = {'high_shelf': {'frequency': 4500, 'gain_db': 6.0, 'quality': 0.707},
           'low_shelf': {'frequency': 150, 'gain_db': f32(-2.16), 'quality': 0.707},
           'max_peaking_eqs': 0, 'peaking_eqs': []}


class FakeCastDevice:
    def __init__(self, uuid, name, model, port, host, control_port=8010, **faults):
        self.uuid, self.name, self.model = uuid, name, model
        self.host, self.port = host, port
        self.lock = threading.RLock()
        self.level, self.muted = 1.0, False
        self.no_pong, self.delay, self.ignore_set_volume = False, 0.0, False
        self.clamp_max, self.round_step = None, None
        self.conns = {}  # socket -> send lock
        self.zc, self.info, self.listener = Zeroconf(), None, None
        self.log = []
        for key, value in faults.items():
            if value is not None:
                setattr(self, key, value)
        self.certdir = tempfile.mkdtemp(prefix='fakecast-')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
                        '-subj', f'/CN={name}', '-keyout', f'{self.certdir}/k.pem',
                        '-out', f'{self.certdir}/c.pem'], check=True, capture_output=True)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(f'{self.certdir}/c.pem', f'{self.certdir}/k.pem')
        self.listen(host, port)
        self.advertise(host, port)
        control = self
        class Control(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def reply(self, code):
                body = json.dumps(control.state()).encode()
                self.send_response(code); self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            def do_GET(self): self.reply(200)
            def do_POST(self):
                try:
                    control.configure(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                except (ValueError, KeyError, TypeError) as exc:
                    self.send_error(400, repr(exc)); return
                self.reply(200)
        self.control = ThreadingHTTPServer(('127.0.0.1', control_port), Control)
        threading.Thread(target=self.control.serve_forever, daemon=True).start()

    # --- state and faults -------------------------------------------------------------------
    def state(self):
        with self.lock:
            return {'level': self.level, 'muted': self.muted, 'no_pong': self.no_pong,
                    'delay': self.delay, 'ignore_set_volume': self.ignore_set_volume,
                    'clamp_max': self.clamp_max, 'round_step': self.round_step,
                    'advertised': self.info is not None, 'host': self.host, 'port': self.port,
                    'connections': len(self.conns), 'log': self.log[-20:]}

    def configure(self, cmd):
        for key in ('no_pong', 'delay', 'ignore_set_volume', 'clamp_max', 'round_step'):
            if key in cmd:
                setattr(self, key, cmd[key])
        if 'set_level' in cmd or 'set_muted' in cmd:
            with self.lock:
                self.level = float(cmd.get('set_level', self.level))
                self.muted = bool(cmd.get('set_muted', self.muted))
            self.broadcast()
        if cmd.get('withdraw'):
            self.withdraw()
        if 'advertise' in cmd:
            adv = cmd['advertise'] or {}
            host, port = adv.get('host', self.host), int(adv.get('port', self.port))
            if (host, port) != (self.host, self.port):
                self.listen(host, port)
            self.advertise(host, port)
        if cmd.get('drop'):
            self.drop()

    def drop(self):
        for conn in list(self.conns):
            # SO_LINGER 0 turns close() into a TCP RST, like a Hub that fell off the network.
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            conn.close()
        self.note('dropped connections')

    def note(self, text):
        self.log.append(f'{time.strftime("%H:%M:%S")} {text}')

    # --- mDNS -------------------------------------------------------------------------------
    def withdraw(self):
        if self.info:
            self.zc.unregister_service(self.info)
            self.info = None
            self.note('mDNS withdrawn')

    def advertise(self, host, port):
        self.withdraw()
        self.host, self.port = host, port
        hexid = self.uuid.hex
        # TXT keys as a real Nest Hub advertises them (cd/rm/bs are opaque ids, made up here).
        props = {'id': hexid, 'cd': hexid.upper(), 'rm': hexid[:16].upper(), 've': '05',
                 'md': self.model, 'ic': '/setup/icon.png', 'fn': self.name, 'ca': '231941',
                 'st': '0', 'bs': hexid[:12].upper(), 'nf': '1', 'rs': ''}
        self.info = ServiceInfo('_googlecast._tcp.local.', f'Fake-Hub-{hexid}._googlecast._tcp.local.',
                                addresses=[socket.inet_aton(host)], port=port, properties=props,
                                server=f'fuchsia-{hexid[:8]}.local.')
        self.zc.register_service(self.info)
        self.note(f'mDNS advertised {host}:{port}')

    # --- Cast wire --------------------------------------------------------------------------
    def listen(self, host, port):
        old, srv = self.listener, socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port)); srv.listen(8)
        self.listener = srv
        threading.Thread(target=self.accept, args=(srv,), daemon=True).start()
        if old:
            # shutdown first: close() alone leaves a thread blocked in accept() holding the port open.
            old.shutdown(socket.SHUT_RDWR)
            old.close()

    def accept(self, srv):
        while True:
            try:
                raw, _ = srv.accept()
                conn = self.ctx.wrap_socket(raw, server_side=True)
            except ssl.SSLError:
                continue
            except OSError:
                return  # listener closed
            self.conns[conn] = threading.Lock()
            self.note(f'accepted connection via {conn.getsockname()[0]}:{conn.getsockname()[1]}')
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def recv(self, conn, n):
        data = b''
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def serve(self, conn):
        try:
            while True:
                msg = CastMessage()
                msg.ParseFromString(self.recv(conn, struct.unpack('>I', self.recv(conn, 4))[0]))
                self.handle(conn, msg)
        except (OSError, ConnectionError):
            pass
        finally:
            self.conns.pop(conn, None)
            conn.close()

    def send(self, conn, msg, delay=None):
        delay = self.delay if delay is None else delay
        def go():
            body = msg.SerializeToString()
            try:
                with self.conns[conn]:
                    conn.sendall(struct.pack('>I', len(body)) + body)
            except (OSError, KeyError):
                pass
        threading.Timer(delay, go).start() if delay else go()

    def reply(self, conn, req, namespace, payload):
        msg = CastMessage(protocol_version=0, source_id='receiver-0', destination_id=req.source_id,
                          namespace=namespace, payload_type=CastMessage.STRING,
                          payload_utf8=json.dumps(payload))
        self.send(conn, msg)

    def status(self, request_id):
        with self.lock:
            # Shape read from a real Nest Hub by cast_readback.py (2026-10-17) while it was casting:
            # no isActiveInput/isStandBy, floats are float32, controlType is 'master'. Its
            # `applications` held the Default Media Receiver; an idle Hub's list is unverified.
            return {'requestId': request_id, 'type': 'RECEIVER_STATUS', 'status': {
                'applications': [], 'version': 2, 'userEq': USER_EQ,
                'volume': {'controlType': 'master', 'level': f32(self.level),
                           'muted': self.muted, 'stepInterval': f32(0.02)}}}

    def broadcast(self):
        """Unsolicited status to every sender, as a Hub sends after a button press."""
        for conn in list(self.conns):
            self.reply(conn, CastMessage(source_id='sender-0'), NS_RECEIVER, self.status(0))

    def handle(self, conn, msg):
        data = json.loads(msg.payload_utf8)
        kind, rid = data.get('type'), data.get('requestId', 0)
        if msg.namespace == NS_HEARTBEAT and kind == 'PING':
            if not self.no_pong:
                self.reply(conn, msg, NS_HEARTBEAT, {'type': 'PONG'})
        elif msg.namespace == NS_RECEIVER and kind == 'GET_STATUS':
            self.reply(conn, msg, NS_RECEIVER, self.status(rid))
        elif msg.namespace == NS_RECEIVER and kind == 'SET_VOLUME':
            self.note(f'SET_VOLUME {data.get("volume")}')
            if self.ignore_set_volume:
                return
            vol = data.get('volume', {})
            with self.lock:
                if 'level' in vol:
                    level = float(vol['level'])
                    if self.round_step:
                        level = round(level / self.round_step) * self.round_step
                    if self.clamp_max is not None:
                        level = min(level, self.clamp_max)
                    self.level = round(min(max(level, 0.0), 1.0), 4)
                if 'muted' in vol:
                    self.muted = bool(vol['muted'])
            self.reply(conn, msg, NS_RECEIVER, self.status(rid))
        # tp.connection CONNECT/CLOSE get no reply on a real device; anything else is ignored.


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--port', type=int, default=8009)
    p.add_argument('--control-port', type=int, default=8010)
    p.add_argument('--host', default='127.0.0.1', help='address to advertise over mDNS')
    p.add_argument('--uuid', default='f4a1c0de-0000-4000-8000-00000000fa4e')
    p.add_argument('--name', default='Fake Hub')
    p.add_argument('--model', default='Google Nest Hub')
    p.add_argument('--no-pong', action='store_true', default=None)
    p.add_argument('--delay', type=float)
    p.add_argument('--ignore-set-volume', action='store_true', default=None)
    p.add_argument('--clamp-max', type=float)
    p.add_argument('--round-step', type=float)
    a = p.parse_args()
    dev = FakeCastDevice(uuidlib.UUID(a.uuid), a.name, a.model, a.port, a.host, a.control_port,
                         no_pong=a.no_pong, delay=a.delay, ignore_set_volume=a.ignore_set_volume,
                         clamp_max=a.clamp_max, round_step=a.round_step)
    print(f'fake cast {a.uuid} "{a.name}" on {a.host}:{a.port}, control http://127.0.0.1:{a.control_port}/',
          flush=True)
    threading.Event().wait()


if __name__ == '__main__':
    main()
