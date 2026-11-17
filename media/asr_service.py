"""Offline socket worker. Only the delivery UID can submit job IDs. One decoder runs at a time."""
import contextlib
import ctypes
import json
import hashlib
import tempfile
import math
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import signal
import stat
import struct
import subprocess
import sys
import threading
import time

# Started with -I from the deployment directory, which is then not on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from recording_receipts import read_regular

SOCKET = Path('/run/psy-asr/transcribe.sock')
INPUT = Path(os.environ['PSY_ASR_INPUT'])
OUTPUT = Path(os.environ['PSY_ASR_OUTPUT'])
EXTENSIONS = set('mp3 wav m4a aac ogg opus flac mp4 mov webm mkv mpga mpeg wma aiff caf'.split())
MAX_TEXT = 16 * 1024 * 1024
POLL_SECONDS = 8
STALL_TIMEOUT = 45 * 60
# One attempt's time slot. Longer recordings resume from the journal in the next attempt,
# after other members' recordings had their turn.
MAX_JOB_TIME = 6 * 3600
ACK_SECONDS = 120
COPY_BLOCK = 1024 ** 2
PR_SET_CHILD_SUBREAPER = 36

DECODER = threading.Lock()
_guard = threading.Lock()
_job_locks = {}
_running = {}


class ASRStalled(TimeoutError):
    """The decoder reached its checkpoint inactivity limit."""


class ASRDeadline(TimeoutError):
    """The attempt used its time slot; the journal keeps every finished window."""


class ClientGone(ConnectionError):
    """The worker closed the connection or the recording was deleted."""


def become_subreaper():
    """Orphaned decoder descendants, including ones that called setsid(), are reparented here."""
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'Cannot become a child subreaper')


def descendants():
    """Every live process below this one, found through parent links in /proc."""
    parents = {}
    for entry in os.scandir('/proc'):
        if not entry.name.isdigit():
            continue
        try:
            with open(f'/proc/{entry.name}/stat', 'rb') as stream:
                fields = stream.read().rsplit(b')', 1)[1].split()
        except (OSError, IndexError):
            continue
        parents.setdefault(int(fields[1]), []).append(int(entry.name))
    found, pending = [], [os.getpid()]
    while pending:
        for child in parents.get(pending.pop(), ()):
            found.append(child)
            pending.append(child)
    return found


def reap():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def kill_tree(child):
    """SIGKILL the decoder's session and everything else below this service, then reap it.

    Only the decoder starts processes here, so nothing below this process is unrelated."""
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 10
    while True:
        alive = descendants()
        for pid in alive:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if child.returncode is None:
            child.wait()
        reap()
        if not alive or time.monotonic() > deadline:
            return
        time.sleep(0.01)


def publish_progress(workdir, dest, previous):
    """Return the high-water mark. Only advances may refresh the public timestamp."""
    try:
        fd = os.open(workdir / 'progress.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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


def decode(workdir, src, target, lang, ident, env, alive=None, statedir=None):
    """Run one sandboxed attempt. workdir is fresh for each attempt and holds its progress file;
    statedir keeps the resumable journal across attempts."""
    cmd = [sys.executable, '-I', str(Path(__file__).with_name('asr_sandbox.py')), str(src), str(target), lang,
           str(statedir or workdir)]
    dest = Path(env['PSY_ASR_PROGRESS']) / (ident + '.json')
    # An attempt can resume its durable transcript/checkpoint. Its old progress
    # mirror must not count as evidence that this attempt has advanced.
    (workdir / 'progress.json').unlink(missing_ok=True)
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
                    if child.poll() is not None:
                        # The decoder exited but a descendant still holds its stdout open.
                        # End the rest of the tree so the remaining output arrives now.
                        kill_tree(child)
                        output = child.communicate(timeout=POLL_SECONDS)[0]
                        break
                    if alive is not None:
                        alive()
                    advanced = publish_progress(workdir, dest, high_water)
                    now = time.monotonic()
                    if advanced > high_water:
                        high_water = advanced
                        last_advance = now
                    if now - last_advance >= STALL_TIMEOUT:
                        raise ASRStalled('ASR made no checkpoint progress')
                    if now - started >= MAX_JOB_TIME:
                        raise ASRDeadline('ASR attempt time slot used')
            if child.returncode:
                raise RuntimeError('ASR decoder failed')
            return json.loads(output.splitlines()[-1])
        finally:
            # ffmpeg can keep stdout open after the Python leader exits, and a descendant
            # may have left the session. Reap the whole tree on completion as well as on timeout.
            kill_tree(child)


def snapshot(jobdir, job, alive=None):
    """Open the upload once without following links, check its signed digest and decode these bytes."""
    ext, size, expected = job['ext'], job['size'], job['content_id']
    if type(size) is not int or not 0 < size <= 1024**3 or not re.fullmatch('[a-f0-9]{64}', expected):
        raise ValueError('Invalid recording bounds')
    source = INPUT / (job['id'] + ext)
    private = jobdir / ('input-' + expected + ext)
    fd = os.open(source,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as src:
        info = os.fstat(src.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise ValueError('Invalid recording file')
        with tempfile.NamedTemporaryFile(dir=jobdir,prefix='input-staging-',delete=False) as temp:
            path = Path(temp.name)
            try:
                blocks, full = hashlib.sha256(), hashlib.sha256()
                remaining = size
                copied = 0
                while remaining:
                    chunk = src.read(min(COPY_BLOCK,remaining))
                    if not chunk:
                        raise ValueError('Incomplete recording')
                    blocks.update(hashlib.sha256(chunk).digest())
                    full.update(chunk)
                    temp.write(chunk)
                    remaining -= len(chunk)
                    copied += 1
                    if alive is not None and copied % 64 == 0:
                        alive()
                if src.read(1) or blocks.hexdigest() != expected:
                    raise ValueError('Recording digest mismatch')
                temp.flush()
                os.fsync(temp.fileno())
                # A stable timestamp keeps the decoder checkpoint key across retries.
                os.utime(path,ns=(0,0))
                os.replace(path,private)
                return private,full.hexdigest()
            finally:
                path.unlink(missing_ok=True)


@contextlib.contextmanager
def job_lock(ident):
    with _guard:
        entry = _job_locks.setdefault(ident, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _guard:
            entry[1] -= 1
            if not entry[1]:
                _job_locks.pop(ident, None)


def watcher(conn, abort):
    """Raise ClientGone once the worker hung up or asked to delete this recording."""
    def alive():
        if abort.is_set():
            raise ClientGone('Recording deleted')
        previous = conn.gettimeout()
        conn.settimeout(0)
        try:
            data = conn.recv(1, socket.MSG_PEEK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            raise ClientGone('Client connection failed') from None
        finally:
            conn.settimeout(previous)
        if not data:
            raise ClientGone('Client disconnected')
    return alive


def remove_job(ident):
    """Drop the private copy, journal, transcript and numeric progress of one recording."""
    jobdir = OUTPUT / ident
    if jobdir.is_symlink():
        jobdir.unlink()
    elif jobdir.exists():
        shutil.rmtree(jobdir)
    progress = os.environ.get('PSY_ASR_PROGRESS')
    if progress:
        (Path(progress) / (ident + '.json')).unlink(missing_ok=True)


def clear_attempts(jobdir):
    for stale in jobdir.glob('attempt-*'):
        if stale.is_dir() and not stale.is_symlink():
            shutil.rmtree(stale)
        else:
            stale.unlink()
    for legacy in ('result.txt', 'progress.json'):
        (jobdir / legacy).unlink(missing_ok=True)


def acknowledged(conn):
    conn.settimeout(ACK_SECONDS)
    data = b''
    try:
        while b'\n' not in data and len(data) < 256:
            chunk = conn.recv(256)
            if not chunk:
                return False
            data += chunk
        return json.loads(data.split(b'\n', 1)[0]).get('saved') is True
    except (OSError, ValueError, AttributeError):
        return False


def transcribe(conn, job, alive):
    ident, ext, lang = job['id'], job['ext'], job['language']
    jobdir = OUTPUT / ident
    jobdir.mkdir(mode=0o700, exist_ok=True)
    if jobdir.is_symlink():
        raise ValueError('Unsafe job directory')
    alive()
    # A fresh directory per attempt: nothing an earlier decoder left can stand in for this result.
    clear_attempts(jobdir)
    attempt = Path(tempfile.mkdtemp(prefix='attempt-', dir=jobdir))
    try:
        src, sha = snapshot(jobdir, job, alive)
        try:
            target = attempt / 'result.txt'
            env = {**os.environ, 'HERMES_HOME': str(jobdir), 'HOME': str(jobdir),
                   'PSY_ASR_INPUT': str(jobdir), 'PSY_ASR_OUTPUT': str(attempt), 'TMPDIR': str(attempt),
                   'PSY_ASR_PROGRESS_FILE': str(attempt / 'progress.json')}
            meta = decode(attempt, src, target, lang, ident, env, alive, jobdir)
            duration = meta['duration']
            # Read through one no-follow descriptor: the checked object is the one sent.
            data = read_regular(target, MAX_TEXT)
        finally:
            src.unlink(missing_ok=True)
    finally:
        shutil.rmtree(attempt, ignore_errors=True)
    if meta.get('complete') is not True or not math.isfinite(duration) or duration <= 0:
        return False
    header = dict(complete=True, duration=duration, speech_detected=meta['speech_detected'],
                  text_bytes=len(data), sha256=sha)
    conn.settimeout(60)
    conn.sendall(json.dumps(header).encode() + b'\n' + data)
    if acknowledged(conn):
        remove_job(ident)
    return True


def serve(conn):
    peer = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
    if struct.unpack('3i', peer)[1] != pwd.getpwnam('psy-deliver').pw_uid:
        return
    conn.settimeout(15)
    raw = b''
    while not raw.endswith(b'\n') and len(raw) <= 512:
        chunk = conn.recv(513 - len(raw))
        if not chunk:
            return
        raw += chunk
    if len(raw) > 512 or not raw.endswith(b'\n'):
        return
    job = json.loads(raw)
    ident = job['id']
    if not isinstance(ident, str) or not re.fullmatch('[0-9a-f]{32}', ident):
        return
    if job.get('action') == 'delete':
        abort = _running.get(ident)
        if abort is not None:
            abort.set()
        with job_lock(ident):
            remove_job(ident)
        conn.sendall(b'{"deleted":true}\n')
        return
    ext, lang = job['ext'], job['language']
    if not re.fullmatch(r'\.[a-z0-9]+', ext) or ext[1:] not in EXTENSIONS or lang not in ('auto', 'ru', 'en'):
        return
    abort = threading.Event()
    alive = watcher(conn, abort)
    with job_lock(ident):
        _running[ident] = abort
        try:
            while not DECODER.acquire(timeout=POLL_SECONDS):
                alive()
            try:
                transcribe(conn, job, alive)
            finally:
                DECODER.release()
        finally:
            _running.pop(ident, None)


def handle(conn):
    with conn:
        try:
            serve(conn)
        except ClientGone:
            # Nobody waits for this result. The journal stays for a retry unless it was deleted.
            print('ASR job abandoned', file=sys.stderr, flush=True)
        except Exception as exc:
            # Child stderr and filesystem paths can include private recording names.
            print('ASR job error:', type(exc).__name__, file=sys.stderr, flush=True)
            try:
                code = ('stalled' if isinstance(exc, ASRStalled) else 'deadline' if isinstance(exc, ASRDeadline)
                        else 'processing_failed')
                conn.sendall(json.dumps(dict(complete=False, error=code)).encode() + b'\n')
            except OSError:
                pass


def main():
    become_subreaper()
    OUTPUT.mkdir(mode=0o700, exist_ok=True, parents=True)
    # Nothing runs yet. Remove snapshots and attempt folders interrupted before their cleanup.
    for abandoned in OUTPUT.glob('*/input-*'):
        if abandoned.is_file() and not abandoned.is_symlink():
            abandoned.unlink()
    for jobdir in OUTPUT.iterdir():
        if jobdir.is_dir() and not jobdir.is_symlink():
            clear_attempts(jobdir)
    SOCKET.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(SOCKET))
        os.chmod(SOCKET, 0o660)
        server.listen(8)
        while True:
            conn, _ = server.accept()
            # A delete request must not wait behind a decode that can run for hours.
            threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == '__main__':
    main()
