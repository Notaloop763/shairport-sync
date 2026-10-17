"""AirPlay PCM to Cast proof of concept; no cross-protocol sync guarantee."""
import argparse
import collections
import json
import math
import os
import queue
import select
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RATE = 48000
FRAME_BYTES = 8  # Shairport pipe defaults: stereo S32_LE / 48 kHz in AirPlay 2 builds
BLOCK = RATE * FRAME_BYTES // 50
stats = {'pcm_bytes': 0, 'nonzero_blocks': 0, 'mp3_bytes': 0, 'clients': 0, 'cast': None}
subscribers = set()
lock = threading.Lock()
volume_cond = threading.Condition()
volume_pending = None  # newest volume command not yet applied
cast_target = None


def map_airplay_volume(value, cap):
    if not math.isfinite(value) or not 0 <= cap <= 1:
        raise ValueError('Invalid volume')
    if value == -144.0:
        return 0.0, True
    if not -30.0 <= value <= 0.0:
        raise ValueError('AirPlay volume outside -30..0 or mute -144')
    requested = (value + 30.0) / 30.0
    return min(requested, cap), requested <= 0


def request_airplay_volume(value):
    """Queue a volume change and return at once; volume_worker applies it."""
    global volume_pending
    cap = float(os.environ.get('CAST_VOLUME_CAP', '0.5'))
    level, muted = map_airplay_volume(value, cap)
    if cast_target is None:
        raise RuntimeError('Cast target not ready')
    command = {'airplay_db': value, 'requested_fraction':
        0.0 if value == -144 else (value + 30) / 30,
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


def feed_pcm(proc, path):
    try:
        os.mkfifo(path, 0o666)
    except FileExistsError:
        if not stat.S_ISFIFO(os.stat(path).st_mode):
            raise RuntimeError('PCM path is not a FIFO')
    os.chmod(path, 0o666)
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
            with lock:
                subscribers.add(q)
                stats['clients'] = len(subscribers)
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
            except (OSError, queue.Empty):
                pass
            finally:
                with lock:
                    subscribers.discard(q)
                    stats['clients'] = len(subscribers)
                self.close_connection = True
        else:
            self.send_error(404)


def cast_audio(host, uuid, stream):
    import uuid as uuidlib
    import pychromecast
    import zeroconf
    zconf = zeroconf.Zeroconf(unicast=True)
    casts, browser = pychromecast.get_listed_chromecasts(
        uuids=[uuidlib.UUID(uuid)], discovery_timeout=20, zeroconf_instance=zconf)
    if not casts:
        raise RuntimeError('Target Cast UUID not discovered')
    cast = casts[0]
    cast.wait(timeout=20)
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
    stats['cast'] = {'uuid': uuid, 'name': cast.cast_info.friendly_name,
                     'host': cast.cast_info.host, 'state': cast.media_controller.status.player_state}
    print('CAST_STARTED', json.dumps(stats['cast']), flush=True)
    # Keep the Cast connection and discovery browser alive for this process.
    return cast, browser


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
    proc = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'warning',
                             '-f', 's32le', '-ar', str(RATE), '-ac', '2', '-i', 'pipe:0',
                             '-c:a', 'libmp3lame', '-b:a', '192k', '-write_xing', '0',
                             '-flush_packets', '1', '-f', 'mp3', 'pipe:1'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
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
    time.sleep(2)
    target, browser = cast_audio(host, os.environ['CAST_UUID'], f'http://{host}:{port}/live.mp3')
    cast_target = target
    while True:
        if proc.poll() is not None:
            raise RuntimeError('Encoder died')
        stats['cast']['state'] = target.media_controller.status.player_state
        time.sleep(5)


if __name__ == '__main__':
    main()
