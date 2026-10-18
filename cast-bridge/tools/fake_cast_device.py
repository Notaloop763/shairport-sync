"""Fake Google Cast receiver for reproducing Nest Hub failures; sits beneath pychromecast.

Needs pychromecast (for its generated CastMessage proto) and zeroconf. Touches only localhost/LAN
sockets of its own, never the real Hub. Advertised on a 127.x host, its mDNS stays on loopback.

What bridge.py asks of a Cast device (pychromecast 14.0.10) and the messages that produces.
"fake" says whether this module answers it.

  discover()      get_chromecasts(timeout=15), stop_discovery   mDNS browse only          fake: advertised
  connect()       Zeroconf(unicast=True)                        mDNS                       fake: advertised
                  get_listed_chromecasts(uuids=[..])            mDNS (TXT id/fn/md, SRV)   fake: advertised
                  cast.wait(timeout=20)                         TLS connect (CERT_NONE),
                                                                tp.connection CONNECT,
                                                                receiver GET_STATUS,
                                                                heartbeat PING every 10 s  fake: answered
  load_stream()   media_controller.launch()                     receiver LAUNCH            fake: answered
                                                                tp.connection CONNECT to
                                                                the app's transportId      fake: no reply, as real
                  media_controller.play_media()                 media LOAD                 fake: answered
                                                                HTTP GET of the LOAD URL,
                                                                as the Hub pulls the stream fake: pulled
                  media_controller.status                       media GET_STATUS           fake: answered
  handle()        media_controller.pause() / play()             media PAUSE / PLAY         fake: answered;
                                                                a paused fake stops reading the stream
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
  {"volume_cap": 0.5}            CAST_VOLUME_CAP guard (default 0.5): a SET_VOLUME above it is
                                 applied but recorded in cap_violations; check_cap() then raises
  {"set_level": 0.7, "set_muted": true}    a physical button: change state, push status to senders
  {"load_error": true}           answer every LOAD with LOAD_FAILED
  {"stop_stream": true}          stop pulling the stream now and report IDLE (idleReason ERROR)
  {"launch_delay": 8.0}          start the app, and answer LAUNCH, N seconds late
  {"takeover": true}             another sender casts: media IDLE (INTERRUPTED), then Spotify's
                                 app replaces ours in RECEIVER_STATUS
  {"close_app": true}            the app quits: tp.connection CLOSE from its transportId, then a
                                 RECEIVER_STATUS with no app
                                 (both sequences follow the Cast docs, unverified on the real Hub)
  {"withdraw": true}             drop the mDNS advertisement
  {"advertise": {"host": "127.0.0.2", "port": 8019}}   (re-)advertise, optionally elsewhere; the
                                 old listener closes and a new one opens on the new port
GET / (any path, e.g. /state) returns the current state as JSON, including the launched App ID
and the LOAD's URL and content type.

On LOAD the fake GETs the URL and keeps every byte in `capture`; playerState goes BUFFERING, then
PLAYING on the first bytes, then IDLE (idleReason ERROR) if the stream ends. It refuses to fetch
anything but loopback, so its runs stay off the LAN. listen(capture) decodes and measures it (needs
ffmpeg and numpy), so a test can tell whether the right audio arrived.

Playout (profile='reference' or 'delayed-7s', see PROFILES; needs ffmpeg and numpy): the pulled MP3
is also decoded into a queue, and a 20 ms playout clock takes blocks from it, applies the device's
gain as it stands at render time (GAIN_LAW), and appends them to `output`, 16-bit mono at
PLAYOUT_RATE starting at monotonic time `output_t0`. PLAYING still follows the first bytes, as it
does on a real device, so `output` is the only evidence of what was heard. Playout faults:
  {"silent_output": true}        render silence whatever arrives
  {"control_delay": 2.0}         volume reaches the output N s after SET_VOLUME (the reply is not
                                 delayed); a huge value is a volume ack that never affects output
  {"gain_at_enqueue": true}      apply gain as audio enters the queue rather than as it plays
  {"render_rate": 0.98}          the playout clock consumes audio this much slower than real time
  {"dropout": 0.5}               zero the next N s of output, once
  {"replay": 2.0}                play the last N s of output again, once

MEDIA_STATUS and the app entry in RECEIVER_STATUS follow Google's Cast message docs; they are not
yet read back from the real Hub.
"""
import argparse
import json
import re
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pychromecast.generated.cast_channel_pb2 import CastMessage
from zeroconf import InterfaceChoice, ServiceInfo, Zeroconf

NS_CONNECTION = 'urn:x-cast:com.google.cast.tp.connection'
NS_HEARTBEAT = 'urn:x-cast:com.google.cast.tp.heartbeat'
NS_RECEIVER = 'urn:x-cast:com.google.cast.receiver'
NS_MEDIA = 'urn:x-cast:com.google.cast.media'
APPS = {'CC1AD845': 'Default Media Receiver', 'CC32E753': 'Spotify'}


def f32(x):
    return struct.unpack('f', struct.pack('f', x))[0]


# Receiver playout models, version 1. None is calibrated against a real Hub: 'delayed-7s' reproduces
# the class of Walter's report (start/pause ~7 s late, volume prompt), not its cause.
PROFILES = {
    'reference': {'version': 1, 'buffer_s': 0.2, 'rebuffer_s': 0.2, 'max_backlog_s': 2.0, 'calibrated': False},
    'delayed-7s': {'version': 1, 'buffer_s': 7.0, 'rebuffer_s': 7.0, 'max_backlog_s': 10.0, 'calibrated': False},
}
PLAYOUT_RATE = 48000
GAIN_LAW = 'amplitude x level, x0 when muted (simulated; a real Hub\'s curve is unmeasured)'

USER_EQ = {'high_shelf': {'frequency': 4500, 'gain_db': 6.0, 'quality': 0.707},
           'low_shelf': {'frequency': 150, 'gain_db': f32(-2.16), 'quality': 0.707},
           'max_peaking_eqs': 0, 'peaking_eqs': []}


def listen(mp3):
    """Decode MP3 bytes; return sample rate, seconds louder than -40 dBFS, loudest 20 ms window's
    level, and the FFT peak of the loud stretch."""
    import numpy as np
    out = subprocess.run(['ffmpeg', '-hide_banner', '-f', 'mp3', '-i', 'pipe:0', '-ac', '1',
                          '-f', 's16le', 'pipe:1'], input=bytes(mp3), capture_output=True, check=True)
    rate = int(re.search(rb'Audio: mp3.*?, (\d+) Hz', out.stderr).group(1))
    x = np.frombuffer(out.stdout, '<i2') / 32768
    win = rate // 50
    n = len(x) // win
    rms = np.sqrt((x[:n * win].reshape(n, win) ** 2).mean(1)) if n else np.zeros(1)
    loud = np.flatnonzero(rms > 10 ** (-40 / 20))
    peak = None
    if len(loud):
        seg = x[loud[0] * win:(loud[-1] + 1) * win]
        peak = float(np.abs(np.fft.rfft(seg * np.hanning(len(seg)))).argmax() * rate / len(seg))
    return {'rate': rate, 'seconds': len(loud) / 50, 'peak_hz': peak,
            'max_dbfs': float(20 * np.log10(max(rms.max(), 1e-10)))}


class FakeCastDevice:
    def __init__(self, uuid, name, model, port, host, control_port=8010, profile=None, **faults):
        self.uuid, self.name, self.model = uuid, name, model
        self.host, self.port = host, port
        self.lock = threading.RLock()
        self.level, self.muted = 1.0, False
        self.no_pong, self.delay, self.ignore_set_volume = False, 0.0, False
        self.clamp_max, self.round_step = None, None
        self.load_error, self.launch_delay = False, 0.0
        self.volume_cap, self.cap_violations = 0.5, []  # the Hub is in Walter's bedroom
        self.app_id, self.session_id = None, None
        self.load, self.player_state, self.idle_reason = None, None, None  # load: the LOAD's media
        # shortcut: capture grows ~24 kB/s at 192 kbit/s without bound; cap it if runs get long.
        self.capture, self.pull_stop = bytearray(), threading.Event()
        self.conns = {}  # socket -> send lock
        self.profile, self.gains = profile, [(time.monotonic(), self.level, self.muted)]
        self.silent_output, self.gain_at_enqueue, self.control_delay, self.render_rate = False, False, 0.0, 1.0
        self.dropout = self.replay = 0.0
        # shortcut: output grows 96 kB/s and history stays 10 s; cap output if runs get long.
        self.queue, self.output, self.history, self.output_t0 = bytearray(), bytearray(), bytearray(), None
        self.buffering, self.started = True, False
        self.decoded = self.rendered = self.underruns = self.dropped = 0  # samples, but underruns
        self.late_max, self.playout_log = 0.0, []  # every 0.5 s: t, received bytes, decoded/queued/rendered s
        # A loopback fake announces on loopback only, so its mDNS never reaches the LAN.
        self.zc = Zeroconf(interfaces=['127.0.0.1'] if host.startswith('127.') else InterfaceChoice.All)
        self.info, self.listener = None, None
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
        if profile:
            threading.Thread(target=self.render, daemon=True).start()

    # --- state and faults -------------------------------------------------------------------
    def state(self):
        with self.lock:
            return {'level': self.level, 'muted': self.muted, 'no_pong': self.no_pong,
                    'delay': self.delay, 'ignore_set_volume': self.ignore_set_volume,
                    'clamp_max': self.clamp_max, 'round_step': self.round_step,
                    'volume_cap': self.volume_cap, 'cap_violations': self.cap_violations,
                    'load_error': self.load_error, 'launch_delay': self.launch_delay,
                    'app_id': self.app_id, 'player_state': self.player_state,
                    'load': self.load and {k: self.load.get(k) for k in
                                           ('contentId', 'contentType', 'streamType')},
                    'capture_bytes': len(self.capture), 'advertised': self.info is not None, 'host': self.host, 'port': self.port,
                    'connections': len(self.conns), 'log': self.log[-20:],
                    'playout': self.profile and self.playout()}

    def playout(self):
        with self.lock:
            return {'profile': self.profile, **PROFILES[self.profile], 'gain_law': GAIN_LAW,
                    'silent_output': self.silent_output, 'control_delay': self.control_delay,
                    'gain_at_enqueue': self.gain_at_enqueue, 'render_rate': self.render_rate,
                    'received_bytes': len(self.capture), 'decoded_s': self.decoded / PLAYOUT_RATE,
                    'queued_s': len(self.queue) / 2 / PLAYOUT_RATE,
                    'rendered_s': self.rendered / PLAYOUT_RATE,
                    'output_s': len(self.output) / 2 / PLAYOUT_RATE, 'underruns': self.underruns,
                    'dropped_s': self.dropped / PLAYOUT_RATE, 'late_max_s': round(self.late_max, 4)}

    def configure(self, cmd):
        for key in ('no_pong', 'delay', 'ignore_set_volume', 'clamp_max', 'round_step',
                    'load_error', 'launch_delay', 'volume_cap', 'silent_output', 'control_delay',
                    'gain_at_enqueue', 'render_rate', 'dropout', 'replay'):
            if key in cmd:
                setattr(self, key, cmd[key])
        if 'set_level' in cmd or 'set_muted' in cmd:
            with self.lock:
                self.level = float(cmd.get('set_level', self.level))
                self.muted = bool(cmd.get('set_muted', self.muted))
                self.gains.append((time.monotonic(), self.level, self.muted))
            self.broadcast()
        if cmd.get('stop_stream'):
            self.pull_stop.set()
            self.stream_ended('stream stopped by control')
        if cmd.get('takeover'):
            self.takeover()
        if cmd.get('close_app'):
            self.close_app()
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

    def check_cap(self):
        """Fail the run if anything asked this device for more than volume_cap."""
        assert not self.cap_violations, f'SET_VOLUME above cap {self.volume_cap}: {self.cap_violations}'

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

    def reply(self, conn, req, namespace, payload, source='receiver-0'):
        msg = CastMessage(protocol_version=0, source_id=source, destination_id=req.source_id,
                          namespace=namespace, payload_type=CastMessage.STRING,
                          payload_utf8=json.dumps(payload))
        self.send(conn, msg)

    def status(self, request_id):
        with self.lock:
            # Shape read from a real Nest Hub by cast_readback.py (2026-10-17) while it was casting:
            # no isActiveInput/isStandBy, floats are float32, controlType is 'master'. Its
            # `applications` held the Default Media Receiver; an idle Hub's list is unverified.
            apps = [{'appId': self.app_id, 'displayName': APPS[self.app_id], 'isIdleScreen': False,
                     'namespaces': [{'name': NS_MEDIA}], 'sessionId': self.session_id,
                     'statusText': APPS[self.app_id], 'transportId': self.session_id}
                    ] if self.app_id else []
            return {'requestId': request_id, 'type': 'RECEIVER_STATUS', 'status': {
                'applications': apps, 'version': 2, 'userEq': USER_EQ,
                'volume': {'controlType': 'master', 'level': f32(self.level),
                           'muted': self.muted, 'stepInterval': f32(0.02)}}}

    def media_status(self, request_id):
        with self.lock:
            status = [{'mediaSessionId': 1, 'playbackRate': 1, 'playerState': self.player_state,
                       'currentTime': 0, 'supportedMediaCommands': 0,
                       'volume': {'level': 1, 'muted': False}, 'media': self.load}
                      ] if self.load else []
            if status and self.idle_reason:
                status[0]['idleReason'] = self.idle_reason
            return {'requestId': request_id, 'type': 'MEDIA_STATUS', 'status': status}

    def broadcast(self):
        """Unsolicited status to every sender, as a Hub sends after a button press."""
        for conn in list(self.conns):
            self.reply(conn, CastMessage(source_id='sender-0'), NS_RECEIVER, self.status(0))

    def broadcast_media(self):
        for conn in list(self.conns):
            self.reply(conn, CastMessage(source_id='*'), NS_MEDIA, self.media_status(0),
                       self.session_id)

    # --- media stream -----------------------------------------------------------------------
    def pull(self, url, stop):
        """GET the LOAD URL as the Hub does and keep the bytes until stopped or the stream ends."""
        dec = None
        if self.profile:
            with self.lock:  # a new stream buffers from empty, as after a LOAD
                self.queue.clear()
                self.buffering, self.started = True, False
            dec = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-probesize', '32',
                                    '-analyzeduration', '0', '-f', 'mp3', '-i', 'pipe:0', '-ac', '1',
                                    '-ar', str(PLAYOUT_RATE), '-flush_packets', '1', '-f', 's16le', 'pipe:1'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
            threading.Thread(target=self.decode, args=(dec, stop), daemon=True).start()
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                while not stop.is_set():
                    if self.player_state == 'PAUSED':  # a paused Hub stops reading its socket
                        time.sleep(0.05)
                        continue
                    chunk = resp.read1(4096)
                    if not chunk:
                        break
                    with self.lock:
                        if stop.is_set():
                            break  # checked under the lock so a stop's IDLE is not overwritten
                        self.capture += chunk
                        first = self.player_state not in ('PLAYING', 'PAUSED')  # one read may land after PAUSE
                        if first:
                            self.player_state = 'PLAYING'
                    if dec:
                        dec.stdin.write(chunk)
                    if first:
                        self.note(f'stream playing from {url}')
                        self.broadcast_media()
        except OSError as exc:
            self.note(f'stream error {exc!r}')
        if dec:
            dec.stdin.close()
        if not stop.is_set():  # else a newer LOAD, LAUNCH or stop_stream took over
            self.stream_ended('stream ended')

    def decode(self, dec, stop):
        import numpy as np
        rest = b''
        while data := dec.stdout.read(8192):
            data, rest = rest + data, b''
            if len(data) % 2:
                data, rest = data[:-1], data[-1:]
            if self.gain_at_enqueue:
                x = np.frombuffer(data, '<i2') * self.gain_at(time.monotonic() - self.control_delay)
                data = x.astype('<i2').tobytes()
            with self.lock:
                if stop.is_set():
                    break  # a newer stream owns the queue
                self.queue += data
                self.decoded += len(data) // 2
        dec.kill()
        dec.wait()

    def gain_at(self, t):
        """GAIN_LAW applied to the device's volume as it stood at monotonic time t."""
        with self.lock:
            level, muted = next((lv, m) for ts, lv, m in reversed(self.gains) if ts <= t) \
                if t >= self.gains[0][0] else self.gains[0][1:]
        return 0.0 if muted else level

    def render(self):
        """Playout clock: every 20 ms take a block from the queue, apply the gain as it stands now
        and append it to `output`; silence while buffering or after an underrun."""
        import numpy as np
        block, p = PLAYOUT_RATE // 50, PROFILES[self.profile]
        self.output_t0 = time.monotonic()
        n = 0
        while True:
            n += 1
            late = time.monotonic() - (self.output_t0 + n / 50)
            if late < 0:
                time.sleep(-late)
            else:
                self.late_max = max(self.late_max, late)
            want = round(block * self.render_rate)
            with self.lock:
                if self.replay:
                    self.queue[:0] = self.history[-int(self.replay * PLAYOUT_RATE) * 2:]
                    self.note(f'replaying the last {self.replay} s'); self.replay = 0.0
                excess = len(self.queue) // 2 - int(p['max_backlog_s'] * PLAYOUT_RATE)
                if excess > 0:
                    del self.queue[:excess * 2]
                    self.dropped += excess
                depth = len(self.queue) // 2
                if self.buffering and depth >= (p['rebuffer_s'] if self.started else p['buffer_s']) * PLAYOUT_RATE:
                    self.buffering, self.started = False, True
                elif not self.buffering and depth < want:
                    self.buffering = True
                    self.underruns += 1
                if self.buffering:
                    x = np.zeros(block)
                else:
                    raw = bytes(self.queue[:want * 2])
                    del self.queue[:want * 2]
                    self.history += raw
                    del self.history[:-10 * PLAYOUT_RATE * 2]
                    self.rendered += want
                    x = np.frombuffer(raw, '<i2').astype(float)
                    if want != block:
                        x = np.interp(np.arange(block) * want / block, np.arange(want), x)
                    if self.dropout > 0:
                        k = min(block, round(self.dropout * PLAYOUT_RATE))
                        x[:k] = 0
                        self.dropout -= k / PLAYOUT_RATE
                gain = 1.0 if self.gain_at_enqueue else self.gain_at(time.monotonic() - self.control_delay)
                if self.silent_output:
                    gain = 0.0
                self.output += (x * gain).astype('<i2').tobytes()
                if n % 25 == 0:
                    self.playout_log.append((round(time.monotonic() - self.output_t0, 2), len(self.capture),
                                             self.decoded / PLAYOUT_RATE, depth / PLAYOUT_RATE,
                                             self.rendered / PLAYOUT_RATE))

    def stream_ended(self, why):
        with self.lock:
            self.player_state, self.idle_reason = 'IDLE', 'ERROR'
        self.note(why)
        self.broadcast_media()

    def takeover(self):
        """Another sender casts (Spotify): our media goes IDLE (INTERRUPTED), then its app replaces ours."""
        self.pull_stop.set()
        with self.lock:
            self.player_state, self.idle_reason = 'IDLE', 'INTERRUPTED'
        self.broadcast_media()
        with self.lock:
            self.app_id, self.session_id = 'CC32E753', str(uuidlib.uuid4())
            self.load = self.player_state = self.idle_reason = None
        self.note('taken over by CC32E753')
        self.broadcast()

    def close_app(self):
        """The app quits ("Hey Google, stop"): it closes its virtual connection and leaves the receiver."""
        self.pull_stop.set()
        with self.lock:
            session, self.app_id, self.session_id = self.session_id, None, None
            self.load = self.player_state = self.idle_reason = None
        for conn in list(self.conns):
            self.reply(conn, CastMessage(source_id='sender-0'), NS_CONNECTION, {'type': 'CLOSE'}, session)
        self.note(f'app closed session {session}')
        self.broadcast()

    def launched(self, conn, msg, app_id, rid):
        with self.lock:
            # Relaunching the running app keeps its session (unverified on the real Hub).
            if self.app_id != app_id:
                self.app_id, self.session_id = app_id, str(uuidlib.uuid4())
                self.load = self.player_state = self.idle_reason = None
                self.pull_stop.set()
        self.reply(conn, msg, NS_RECEIVER, self.status(rid))

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
                    if self.volume_cap is not None and level > self.volume_cap:
                        self.cap_violations.append(level)
                        self.note(f'CAP VIOLATION {level} > {self.volume_cap}')
                    if self.round_step:
                        level = round(level / self.round_step) * self.round_step
                    if self.clamp_max is not None:
                        level = min(level, self.clamp_max)
                    self.level = round(min(max(level, 0.0), 1.0), 4)
                if 'muted' in vol:
                    self.muted = bool(vol['muted'])
                self.gains.append((time.monotonic(), self.level, self.muted))
            self.reply(conn, msg, NS_RECEIVER, self.status(rid))
        elif msg.namespace == NS_RECEIVER and kind == 'LAUNCH':
            self.note(f'LAUNCH {data.get("appId")}')
            if data.get('appId') not in APPS:
                self.reply(conn, msg, NS_RECEIVER, {'requestId': rid, 'type': 'LAUNCH_ERROR',
                                                    'reason': 'NOT_FOUND'})
                return
            args = (conn, msg, data['appId'], rid)
            threading.Timer(self.launch_delay, self.launched, args).start() \
                if self.launch_delay else self.launched(*args)
        elif msg.namespace == NS_MEDIA and kind == 'GET_STATUS':
            self.reply(conn, msg, NS_MEDIA, self.media_status(rid), self.session_id)
        elif msg.namespace == NS_MEDIA and kind == 'LOAD':
            media = data.get('media', {})
            url = media.get('contentId', '')
            self.note(f'LOAD {url} {media.get("contentType")}')
            if self.load_error or not (urllib.parse.urlsplit(url).hostname or '').startswith('127.'):
                self.reply(conn, msg, NS_MEDIA, {'requestId': rid, 'type': 'LOAD_FAILED'},
                           self.session_id)
                return
            with self.lock:
                self.load, self.player_state, self.idle_reason = media, 'BUFFERING', None
                self.pull_stop.set()
                self.pull_stop = threading.Event()
            threading.Thread(target=self.pull, args=(url, self.pull_stop), daemon=True).start()
            self.reply(conn, msg, NS_MEDIA, self.media_status(rid), self.session_id)
        elif msg.namespace == NS_MEDIA and kind in ('PAUSE', 'PLAY') and self.load:
            self.note(kind)
            with self.lock:
                self.player_state = 'PAUSED' if kind == 'PAUSE' else 'PLAYING'
            self.reply(conn, msg, NS_MEDIA, self.media_status(rid), self.session_id)
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
    p.add_argument('--load-error', action='store_true', default=None)
    p.add_argument('--launch-delay', type=float)
    p.add_argument('--volume-cap', type=float)
    p.add_argument('--profile', choices=PROFILES)
    a = p.parse_args()
    dev = FakeCastDevice(uuidlib.UUID(a.uuid), a.name, a.model, a.port, a.host, a.control_port,
                         no_pong=a.no_pong, delay=a.delay, ignore_set_volume=a.ignore_set_volume,
                         clamp_max=a.clamp_max, round_step=a.round_step,
                         load_error=a.load_error, launch_delay=a.launch_delay,
                         volume_cap=a.volume_cap, profile=a.profile)
    print(f'fake cast {a.uuid} "{a.name}" on {a.host}:{a.port}, control http://127.0.0.1:{a.control_port}/',
          flush=True)
    threading.Event().wait()


if __name__ == '__main__':
    main()
