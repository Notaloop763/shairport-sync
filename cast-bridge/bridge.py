"""AirPlay PCM to Cast proof of concept; no cross-protocol sync guarantee."""
import argparse
import collections
import json
import math
import os
import queue
import re
import select
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RATE = 48000
FRAME_BYTES = 8  # Shairport pipe defaults: stereo S32_LE / 48 kHz in AirPlay 2 builds
BLOCK = RATE * FRAME_BYTES // 50
stats = {'pcm_bytes': 0, 'nonzero_blocks': 0, 'mp3_bytes': 0, 'clients': 0, 'cast': None,
         'sent_bytes': {}}  # per open /live.mp3 connection, keyed 'ip:port'
MP3_BYTES_PER_S = 24000  # libmp3lame CBR 192k at 48 kHz: 576-byte frames, no padding
# No input probing: by default ffmpeg holds the first 5 s of PCM to analyse it, then sends it all
# at once, and a Hub that connects meanwhile keeps that burst as extra playout delay.
ENCODER = ['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-probesize', '32', '-analyzeduration', '0',
           '-f', 's32le', '-ar', str(RATE), '-ac', '2', '-i', 'pipe:0',
           '-c:a', 'libmp3lame', '-b:a', '192k', '-write_xing', '0',
           '-flush_packets', '1', '-f', 'mp3', 'pipe:1']
subscribers = set()
lock = threading.Lock()
volume_cond = threading.Condition()
volume_pending = None  # newest volume command not yet applied
cast_target = None
paused_by_us = False  # we sent PAUSE for an AirPlay pause or flush; the recovery loop must not undo it
needs_reload = False  # AirPlay flushed, so the Hub still holds pre-flush audio: resume with a new LOAD
cast_lock = threading.Lock()  # the event thread and the recovery loop both LOAD; one at a time


def map_airplay_volume(value, cap):
    if not math.isfinite(value) or not 0 <= cap <= 1:
        raise ValueError('Invalid volume')
    if value == -144.0:
        return 0.0, True, 0.0
    if not -30.0 <= value <= 0.0:
        raise ValueError('AirPlay volume outside -30..0 or mute -144')
    requested = (value + 30.0) / 30.0
    return min(requested, cap), requested <= 0, requested


def request_airplay_volume(value):
    """Queue a volume change and return at once; volume_worker applies it."""
    global volume_pending
    cap = float(os.environ.get('CAST_VOLUME_CAP', '0.5'))
    level, muted, requested = map_airplay_volume(value, cap)
    if cast_target is None:
        raise RuntimeError('Cast target not ready')
    command = {'airplay_db': value, 'requested_fraction': requested,
        'target_fraction': level, 'muted': muted, 'cap': cap,
        'received_at': time.time()}
    with volume_cond:
        # Shairport forks a hook per slider tick without waiting, so only the
        # newest value matters; older ones still pending are dropped.
        volume_pending = command
        stats['volume_command'] = command
        volume_cond.notify()
    print('VOLUME_COMMAND', json.dumps(command), flush=True)
    return command


def apply_volume(level, muted):
    # Skip calls the device already matches; each costs a Cast round-trip.
    status = cast_target.status
    if status is None or abs(status.volume_level - level) > 0.001:
        cast_target.set_volume(level)
    if status is None or status.volume_muted != muted:
        cast_target.set_volume_muted(muted)


VOLUME_ATTEMPTS = 3
VOLUME_RETRY_DELAY = 1.0
VOLUME_RECONNECT_WAIT = 60.0  # pychromecast took 1-155 s to reconnect in fake-Hub runs


def volume_worker():
    global volume_pending
    command, attempts = None, 0
    while True:
        with volume_cond:
            if command is None:
                while volume_pending is None:
                    volume_cond.wait()
            else:
                # Retry the failed command unless a newer one arrives first.
                volume_cond.wait_for(lambda: volume_pending is not None, VOLUME_RETRY_DELAY)
            if volume_pending is not None:
                command, volume_pending, attempts = volume_pending, None, 0
        attempts += 1
        try:
            apply_volume(command['target_fraction'], command['muted'])
        except Exception as exc:
            # A failure between the two calls leaves level and mute out of step;
            # the retry compares against the device status and sends what is left.
            stats['volume_error'] = {'error': repr(exc), 'airplay_db': command['airplay_db'],
                                     'attempt': attempts, 'at': time.time()}
            print('VOLUME_FAILURE', json.dumps(stats['volume_error']), flush=True)
            reconnecting = not getattr(getattr(cast_target, 'socket_client', None), 'is_connected', True)
            if reconnecting and time.time() - command['received_at'] < VOLUME_RECONNECT_WAIT:
                attempts -= 1  # never reached the device; keep trying while pychromecast reconnects
            if attempts >= VOLUME_ATTEMPTS:
                command = None
            continue
        stats['volume_applied'] = dict(command, applied_at=time.time())
        stats.pop('volume_error', None)
        command = None


def discover():
    import pychromecast
    casts, browser = pychromecast.get_chromecasts(timeout=15)
    try:
        result = []
        for cast in casts:
            i = cast.cast_info
            result.append({'name': i.friendly_name, 'model': i.model_name,
                           'uuid': str(i.uuid), 'host': i.host, 'port': i.port})
        return result
    finally:
        pychromecast.discovery.stop_discovery(browser)


def make_fifo(path):
    try:
        os.mkfifo(path, 0o666)
    except FileExistsError:
        if not stat.S_ISFIFO(os.stat(path).st_mode):
            raise RuntimeError(f'{path} is not a FIFO')
    os.chmod(path, 0o666)


def feed_pcm(proc, path):
    make_fifo(path)
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    pending = bytearray()
    stats.update(pcm_blocks=0, input_waits=0, idle_events=0,
                 inserted_silence_bytes=0, input_stalls=0)
    last_input = time.monotonic()
    idle = False
    # Shairport already paces playback. Do not impose a second clock or replace
    # short FIFO reads with zeros. Backpressure and the FIFO preserve all samples.
    while proc.poll() is None:
        ready, _, _ = select.select([fd], [], [], 0.02 if idle else 0.1)
        if ready:
            chunk = os.read(fd, BLOCK * 8)
            if chunk:
                if idle:
                    stats['input_resumed_monotonic'] = time.monotonic()
                pending.extend(chunk)
                stats['pcm_bytes'] += len(chunk)
                last_input = time.monotonic()
                idle = False
        else:
            stats['input_waits'] += 1
        while len(pending) >= BLOCK:
            data = bytes(pending[:BLOCK])
            del pending[:BLOCK]
            if any(data):
                stats['nonzero_blocks'] += 1
                stats['last_audio_monotonic'] = time.monotonic()
            proc.stdin.write(data)
            proc.stdin.flush()
            stats['pcm_blocks'] += 1
        if time.monotonic() - last_input >= 0.5:
            if not idle:
                stats['idle_events'] += 1
                stats['input_stalls'] += 1
                idle = True
            # Keep the Cast connection alive only after a genuine input stop.
            # Retain partial input bytes; never discard a partial PCM frame.
            proc.stdin.write(bytes(BLOCK))
            proc.stdin.flush()
            stats['inserted_silence_bytes'] += BLOCK
    raise RuntimeError('Encoder exited')


ITEM = re.compile(rb'<item><type>([0-9a-f]+)</type><code>([0-9a-f]+)</code>')


def read_events(path, handle):
    """Call handle(code) for each ssnc item Shairport writes to the metadata pipe, e.g. 'paus'."""
    make_fifo(path)
    fd = os.open(path, os.O_RDWR)  # O_RDWR: no EOF while Shairport is between writers
    pending = b''
    while True:
        pending += os.read(fd, 4096)
        *items, pending = pending.split(b'</item>')
        for item in items:
            match = ITEM.search(item)
            if match and match[1] == b'73736e63':  # 'ssnc', Shairport's own session events
                handle(bytes.fromhex(match[2].decode()).decode('ascii', 'replace'))


def handle(code, stream):
    """The translation table: one AirPlay event in, Cast verbs out."""
    global paused_by_us, needs_reload
    if cast_target is None or code not in ('paus', 'pfls', 'pres'):
        return
    print('AIRPLAY_EVENT', code, flush=True)
    try:
        # Another app or sender owns the Hub: PAUSE or PLAY would hit their media. The recovery
        # loop recasts on new AirPlay audio, as Walter's policy says.
        if chosen_elsewhere(cast_target, cast_target.media_controller.status, stream):
            return
        if code == 'pres':
            paused_by_us = False
            if needs_reload:
                with cast_lock:
                    load_stream(cast_target, stream)
            else:
                cast_target.media_controller.play()
        else:
            needs_reload = needs_reload or code == 'pfls'
            if not paused_by_us:  # an iPhone pause sends paus then pfls: one PAUSE
                paused_by_us = True
                cast_target.media_controller.pause()
    except Exception as exc:
        # A failed LOAD or PLAY leaves the Hub not playing, which the recovery loop in main() repairs.
        paused_by_us = False
        print('EVENT_FAILURE', json.dumps({'code': code, 'error': repr(exc)}), flush=True)


def broadcast(proc):
    while True:
        data = proc.stdout.read1(2048)
        if not data:
            os._exit(1)
        stats['mp3_bytes'] += len(data)
        with lock:
            for q in tuple(subscribers):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    subscribers.remove(q)
                    # Slow clients are disconnected rather than accumulating stale audio.
                    while not q.empty():
                        q.get_nowait()
                    q.put_nowait(None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_POST(self):
        if self.path != '/airplay-volume':
            self.send_error(404)
            return
        if self.client_address[0] not in ('127.0.0.1', os.environ.get('BRIDGE_HOST')):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 64:
                raise ValueError('Invalid body size')
            value = float(self.rfile.read(length).decode('ascii').strip())
            result = request_airplay_volume(value)
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        except RuntimeError as exc:
            self.send_error(503, str(exc))
            return
        data = json.dumps(result).encode()
        self.send_response(202)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ('/health', '/status'):
            data = json.dumps(stats.copy()).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == '/live.mp3':
            q = queue.Queue(maxsize=128)
            key = '%s:%d' % self.client_address[:2]
            with lock:
                subscribers.add(q)
                stats['clients'] = len(subscribers)
                stats['sent_bytes'][key] = 0
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.connection.settimeout(10)
            try:
                while True:
                    data = q.get(timeout=15)
                    if data is None:
                        break
                    self.wfile.write(data)
                    self.wfile.flush()
                    stats['sent_bytes'][key] += len(data)
            except (OSError, queue.Empty):
                pass
            finally:
                with lock:
                    subscribers.discard(q)
                    stats['clients'] = len(subscribers)
                    del stats['sent_bytes'][key]
                self.close_connection = True
        else:
            self.send_error(404)


def connect(uuid):
    import uuid as uuidlib
    import pychromecast
    import zeroconf
    zconf = zeroconf.Zeroconf(unicast=True)
    casts, browser = pychromecast.get_listed_chromecasts(
        uuids=[uuidlib.UUID(uuid)], discovery_timeout=20, zeroconf_instance=zconf)
    if not casts:
        pychromecast.discovery.stop_discovery(browser)
        zconf.close()
        raise RuntimeError('Target Cast UUID not discovered')
    cast = casts[0]
    cast.register_connection_listener(DropWatch())
    cast.wait(timeout=20)
    return cast, browser


class DropWatch:
    """Notes when the Cast connection drops, as it does when the Hub reboots or loses Wi-Fi."""
    def new_connection_status(self, status):
        if status.status != 'CONNECTED':
            stats['cast_dropped_monotonic'] = time.monotonic()


def load_stream(cast, stream):
    global paused_by_us, needs_reload
    paused_by_us = needs_reload = False  # a LOAD starts fresh playback: no pause or stale buffer left
    # Preserve the user's current device volume across bridge restarts.
    cast.media_controller.launch()
    time.sleep(2)
    cast.media_controller.play_media(stream, 'audio/mpeg', title='AirPlay Bridge', stream_type='LIVE')
    for _ in range(40):
        status = cast.media_controller.status
        if status.content_id == stream and status.player_state == 'PLAYING' and stats['clients'] > 0:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError(f'Cast failed to load bridge stream: {cast.media_controller.status}')
    stats['cast'] = {'uuid': str(cast.uuid), 'name': cast.cast_info.friendly_name,
                     'host': cast.cast_info.host, 'state': cast.media_controller.status.player_state}
    print('CAST_STARTED', json.dumps(stats['cast']), flush=True)


RECAST_ATTEMPTS = 5
RECAST_DELAYS = (5, 10, 20, 40)  # seconds after each failed attempt; Walter's policy
LOST_AFTER = 10.0  # BUFFERING blips are normal; only a longer absence of PLAYING is a loss
STEADY_PLAY = 60.0  # this long playing again restores the attempt budget


def chosen_elsewhere(cast, status, stream):
    """True when a person most likely stopped us: another app, no app, a stop, or another LOAD."""
    return (cast.app_id != 'CC1AD845' or status.idle_reason in ('CANCELLED', 'INTERRUPTED')
            or status.content_id not in (None, stream))


def new_airplay_audio(since):
    """AirPlay input resumed after `since` and carried sound: someone pressed play."""
    return (stats.get('input_resumed_monotonic', 0) > since
            and stats.get('last_audio_monotonic', 0) > since)


def main():
    global cast_target
    parser = argparse.ArgumentParser()
    parser.add_argument('--discover', action='store_true')
    args = parser.parse_args()
    if args.discover:
        print(json.dumps(discover(), indent=2))
        return
    host = os.environ['BRIDGE_HOST']
    port = int(os.environ.get('BRIDGE_PORT', '18765'))
    proc = subprocess.Popen(ENCODER, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    def guarded_feed():
        try:
            feed_pcm(proc, '/audio/pcm')
        except Exception as exc:
            print('PCM_FAILURE', repr(exc), flush=True)
            os._exit(1)
    threading.Thread(target=guarded_feed, daemon=True).start()
    threading.Thread(target=broadcast, args=(proc,), daemon=True).start()
    threading.Thread(target=volume_worker, daemon=True).start()
    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stream = f'http://{host}:{port}/live.mp3'
    threading.Thread(target=read_events, args=('/audio/metadata', lambda code: handle(code, stream)),
                     daemon=True).start()
    time.sleep(2)
    # Walter's recovery policy: recast at once when the Hub drops our stream; when a person chose
    # something else, wait for new AirPlay audio. RECAST_ATTEMPTS tries, then wait for new audio.
    target = browser = None  # keep the Cast connection and discovery browser alive for this process
    lost_since, confirmed, wait_audio_since = time.monotonic(), True, None  # nothing cast yet
    attempts, next_try, playing_since = 0, 0.0, None
    while True:
        if proc.poll() is not None:
            raise RuntimeError('Encoder died')
        now = time.monotonic()
        status = target.media_controller.status if target else None
        if status and stats['cast']:
            stats['cast']['state'] = status.player_state
            # Audio the Hub holds unplayed: seconds sent on its one connection minus its position.
            # Negative means the Hub reopened the stream without a new LOAD; discard that reading.
            with lock:
                sent = [n for k, n in stats['sent_bytes'].items()
                        if k.rpartition(':')[0] == stats['cast']['host']]
            if len(sent) == 1 and status.adjusted_current_time is not None:
                stats['cast']['lag_s'] = round(sent[0] / MP3_BYTES_PER_S - status.adjusted_current_time, 2)
            else:
                stats['cast'].pop('lag_s', None)
        if status and status.content_id == stream and (status.player_state in ('PLAYING', 'BUFFERING')
                                                       or (paused_by_us and status.player_state == 'PAUSED')):
            lost_since = wait_audio_since = None
            playing_since = playing_since or now
            if now - playing_since >= STEADY_PLAY:
                attempts = 0
        else:
            playing_since = None
            if lost_since is None:
                lost_since, confirmed = now, False
            if target and not target.socket_client.is_connected:
                time.sleep(5)  # pychromecast is reconnecting; attempts now would only use up the budget
                continue
            if not confirmed and now - lost_since >= LOST_AFTER:
                confirmed = True
                # A drop resets the media status at once, so it lands within one poll before the loss.
                dropped = stats.get('cast_dropped_monotonic', 0) >= lost_since - 5
                if not dropped and chosen_elsewhere(target, status, stream):
                    wait_audio_since = lost_since
                print('CAST_LOST', json.dumps({'state': status.player_state, 'idle_reason': status.idle_reason,
                      'app_id': target.app_id, 'dropped': dropped,
                      'waiting_for_audio': wait_audio_since is not None}), flush=True)
            if confirmed and wait_audio_since is None and attempts >= RECAST_ATTEMPTS:
                wait_audio_since = now
                print('RECAST_GAVE_UP', json.dumps({'attempts': attempts}), flush=True)
            if wait_audio_since is not None and new_airplay_audio(wait_audio_since):
                wait_audio_since, attempts, next_try = None, 0, now
            if confirmed and wait_audio_since is None and now >= next_try:
                attempts += 1
                try:
                    if target is None:
                        target, browser = connect(os.environ['CAST_UUID'])
                        cast_target = target
                    with cast_lock:
                        load_stream(target, stream)
                except Exception as exc:
                    print('RECAST_FAILURE', json.dumps({'attempt': attempts, 'error': repr(exc)}), flush=True)
                    next_try = time.monotonic() + RECAST_DELAYS[min(attempts, len(RECAST_DELAYS)) - 1]
                else:
                    lost_since = None
                continue
        time.sleep(5)


if __name__ == '__main__':
    main()
