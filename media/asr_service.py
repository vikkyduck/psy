"""Offline socket worker. Only the uploader UID can submit job IDs."""
import json
import math
import os
from pathlib import Path
import pwd
import re
import socket
import signal
import stat
import struct
import subprocess
import sys
import time

SOCKET = Path('/run/psy-asr/transcribe.sock')
INPUT = Path(os.environ['PSY_ASR_INPUT'])
OUTPUT = Path(os.environ['PSY_ASR_OUTPUT'])
EXTENSIONS = set('mp3 wav m4a aac ogg opus flac mp4 mov webm mkv mpga mpeg wma aiff caf'.split())
MAX_TEXT = 16 * 1024 * 1024
POLL_SECONDS = 8
STALL_TIMEOUT = 45 * 60
MAX_JOB_TIME = 6 * 3600


class ASRStalled(TimeoutError):
    """The decoder reached its checkpoint inactivity limit."""


def publish_progress(jobdir, dest, previous):
    """Return the high-water mark. Only advances may refresh the public timestamp."""
    try:
        fd = os.open(jobdir / 'progress.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return previous
            item = json.loads(stream.read(1025))
        done, duration = item['processed_seconds'], item['total_seconds']
    except (OSError, ValueError, KeyError, TypeError):
        return previous
    if not all(type(x) in (int, float) and math.isfinite(x) for x in (done, duration)):
        return previous
    if not previous < done <= duration <= 86400:
        return previous
    tmp = dest.with_suffix('.part')
    tmp.write_text(json.dumps(dict(processed_seconds=done, total_seconds=duration)))
    tmp.chmod(0o640)
    tmp.replace(dest)
    return done


def decode(jobdir, src, target, lang, ident, env):
    cmd = [sys.executable, '-I', str(Path(__file__).with_name('asr_sandbox.py')), str(src), str(target), lang]
    dest = Path(env['PSY_ASR_PROGRESS']) / (ident + '.json')
    # An attempt can resume its durable transcript/checkpoint. Its old progress
    # mirror must not count as evidence that this attempt has advanced.
    (jobdir / 'progress.json').unlink(missing_ok=True)
    dest.unlink(missing_ok=True)
    with subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          start_new_session=True) as child:
        started = last_advance = time.monotonic()
        high_water = 0
        try:
            while True:
                try:
                    output = child.communicate(timeout=POLL_SECONDS)[0]
                    break
                except subprocess.TimeoutExpired:
                    advanced = publish_progress(jobdir, dest, high_water)
                    now = time.monotonic()
                    if advanced > high_water:
                        high_water = advanced
                        last_advance = now
                    if now - last_advance >= STALL_TIMEOUT:
                        raise ASRStalled('ASR made no checkpoint progress')
                    if now - started >= MAX_JOB_TIME:
                        raise TimeoutError('ASR deadline exceeded')
            if child.returncode:
                raise RuntimeError('ASR decoder failed')
            return json.loads(output.splitlines()[-1])
        finally:
            # ffmpeg can keep stdout open after the Python leader exits.
            # Reap the whole session on completion as well as on timeout.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


def serve(conn):
    peer = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
    if struct.unpack('3i', peer)[1] != pwd.getpwnam('psy-deliver').pw_uid:
        return
    conn.settimeout(15)
    with conn.makefile('rb') as stream:
        raw = stream.readline(513)
    if len(raw) > 512 or not raw.endswith(b'\n'):
        return
    job = json.loads(raw)
    ident, ext, lang = job['id'], job['ext'], job['language']
    if not re.fullmatch('[0-9a-f]{32}', ident) or not re.fullmatch(r'\.[a-z0-9]+', ext) or ext[1:] not in EXTENSIONS or lang not in ('auto', 'ru', 'en'):
        return
    src = INPUT / (ident + ext)
    st = src.lstat()
    if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= 1024 ** 3:
        return
    jobdir = OUTPUT / ident
    jobdir.mkdir(mode=0o700, exist_ok=True)
    if jobdir.is_symlink():
        raise ValueError('Unsafe job directory')
    target = jobdir / 'result.txt'
    env = {**os.environ, 'HERMES_HOME': str(jobdir), 'HOME': str(jobdir),
           'PSY_ASR_OUTPUT': str(jobdir), 'TMPDIR': str(jobdir)}
    try:
        env['PSY_ASR_PROGRESS_FILE'] = str(jobdir / 'progress.json')
        meta = decode(jobdir, src, target, lang, ident, env)
        duration = meta['duration']
        if not stat.S_ISREG(target.lstat().st_mode):
            raise ValueError('Unsafe result')
        count = target.stat().st_size
        if not meta['complete'] or not math.isfinite(duration) or duration <= 0 or not 0 < count <= MAX_TEXT:
            return
        header = dict(complete=True, duration=duration, speech_detected=meta['speech_detected'], text_bytes=count)
        conn.sendall(json.dumps(header).encode() + b'\n')
        with target.open('rb') as file:
            conn.sendfile(file)
    finally:
        target.unlink(missing_ok=True)


def main():
    OUTPUT.mkdir(mode=0o700, exist_ok=True, parents=True)
    SOCKET.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(SOCKET))
        os.chmod(SOCKET, 0o660)
        server.listen(4)
        while True:
            conn, _ = server.accept()
            with conn:
                try:
                    serve(conn)
                except Exception as exc:
                    # Child stderr and filesystem paths can include private recording names.
                    print('ASR job error:', type(exc).__name__, file=sys.stderr, flush=True)
                    try:
                        code = 'stalled' if isinstance(exc, ASRStalled) else 'processing_failed'
                        conn.sendall(json.dumps(dict(complete=False, error=code)).encode() + b'\n')
                    except OSError:
                        pass


if __name__ == '__main__':
    main()
