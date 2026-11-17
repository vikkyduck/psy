"""The ASR socket service: process cleanup, disconnects, time slots, result boundary and deletion."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

with patch.dict(os.environ, PSY_ASR_INPUT='/unused', PSY_ASR_OUTPUT='/unused'):
    import asr_service as asr

ESCAPE = """
import json, os, sys, time
pid = os.fork()
if pid == 0:
    os.setsid()
    if os.fork():
        os._exit(0)
    with open(sys.argv[1], 'w') as f:
        f.write(str(os.getpid()))
    time.sleep(60)
    os._exit(0)
os.waitpid(pid, 0)
while not os.path.exists(sys.argv[1]):
    time.sleep(0.01)
if sys.argv[2] == 'finish':
    print(json.dumps(dict(complete=True, duration=1.0)))
    sys.exit(0)
time.sleep(60)
"""


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    status = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
    return status.returncode != 0 or status.stdout.strip().startswith('Z')


class DecoderProcesses(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The service does this at start; orphans then become our children, not init's.
        asr.become_subreaper()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.public = self.root / 'public'
        self.public.mkdir()
        self.env = {**os.environ, 'PSY_ASR_PROGRESS': str(self.public)}

    def spawn(self, script, *args):
        real = subprocess.Popen
        return patch.object(asr.subprocess, 'Popen',
                            side_effect=lambda _cmd, **kw: real([sys.executable, '-c', script, *args], **kw))

    def test_descendant_that_left_the_session_is_killed_after_success_and_after_stall(self):
        for mode in ('finish', 'stall'):
            with self.subTest(mode=mode):
                marker = self.root / ('escaped-' + mode)
                with self.spawn(ESCAPE, str(marker), mode), patch.object(asr, 'POLL_SECONDS', 0.05), \
                        patch.object(asr, 'STALL_TIMEOUT', 1.5):
                    if mode == 'finish':
                        self.assertTrue(asr.decode(self.root, Path('x'), Path('y'), 'ru', 'a'*32, self.env)['complete'])
                    else:
                        with self.assertRaises(asr.ASRStalled):
                            asr.decode(self.root, Path('x'), Path('y'), 'ru', 'a'*32, self.env)
                pid = int(marker.read_text())
                self.assertTrue(gone(pid), 'setsid descendant survived')
                self.assertEqual(asr.descendants(), [])

    def test_closed_client_connection_aborts_the_decoder(self):
        mine, worker = socket.socketpair()
        self.addCleanup(mine.close)
        mine.settimeout(15)
        alive = asr.watcher(mine, threading.Event())
        alive()
        worker.close()
        started = time.monotonic()
        with self.spawn('import time; time.sleep(60)'), patch.object(asr, 'POLL_SECONDS', 0.05):
            with self.assertRaises(asr.ClientGone):
                asr.decode(self.root, Path('x'), Path('y'), 'ru', 'a'*32, self.env, alive)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(mine.gettimeout(), 15)
        self.assertEqual(asr.descendants(), [])

    def test_time_slot_ends_attempt_with_its_own_error(self):
        progress = self.root / 'progress.json'
        script = """
import json, sys, time
for done in range(1, 10000):
    with open(sys.argv[1] + '.tmp', 'w') as f:
        json.dump(dict(processed_seconds=done, total_seconds=20000), f)
    import os; os.replace(sys.argv[1] + '.tmp', sys.argv[1])
    time.sleep(0.02)
"""
        with self.spawn(script, str(progress)), patch.object(asr, 'POLL_SECONDS', 0.05), \
                patch.object(asr, 'MAX_JOB_TIME', 0.4):
            with self.assertRaises(asr.ASRDeadline):
                asr.decode(self.root, Path('x'), Path('y'), 'ru', 'b'*32, self.env)


class ServiceRequests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.input, self.output, self.progress = root / 'in', root / 'out', root / 'progress'
        for path in (self.input, self.output, self.progress):
            path.mkdir()
        self.canary = root / 'canary.txt'
        self.canary.write_text('private text of another recording')
        for scope in (patch.object(asr, 'INPUT', self.input), patch.object(asr, 'OUTPUT', self.output),
                      patch.object(asr.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid())),
                      patch.dict(os.environ, PSY_ASR_PROGRESS=str(self.progress))):
            scope.start()
            self.addCleanup(scope.stop)
        self.audio = b'synthetic recording bytes'
        self.ident = 'c' * 32
        (self.input / (self.ident + '.wav')).write_bytes(self.audio)
        self.job = {'id': self.ident, 'ext': '.wav', 'language': 'ru', 'size': len(self.audio),
                    'content_id': hashlib.sha256(hashlib.sha256(self.audio).digest()).hexdigest()}
        self.attempts = []

    def fake_decode(self, result):
        def decode(workdir, src, target, lang, ident, env, alive=None, statedir=None):
            self.attempts.append(workdir)
            self.assertEqual(list(workdir.iterdir()), [])
            self.assertEqual(src.read_bytes(), self.audio)
            journal = statedir / 'media-vault' / 'journal.jsonl'
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text('{}\n')
            (self.progress / (ident + '.json')).write_text('{}')
            result(target)
            return {'complete': True, 'duration': 2.5, 'speech_detected': True}
        return patch.object(asr, 'decode', decode)

    def request(self, payload, ack=None):
        mine, theirs = socket.socketpair()
        thread = threading.Thread(target=asr.handle, args=(theirs,))
        thread.start()
        mine.settimeout(10)
        with mine, mine.makefile('rb') as stream:
            mine.sendall(json.dumps(payload).encode() + b'\n')
            header = json.loads(stream.readline() or b'null')
            body = stream.read(header['text_bytes']) if header and header.get('text_bytes') else None
            if ack is not None:
                mine.sendall(ack)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        return header, body

    def test_saved_result_removes_private_copy_journal_and_progress(self):
        jobdir = self.output / self.ident
        jobdir.mkdir()
        stale = jobdir / 'attempt-old'
        stale.mkdir()
        (stale / 'result.txt').write_text('planted by an earlier attempt')
        with self.fake_decode(lambda target: target.write_text('fresh transcript')):
            header, body = self.request(self.job, ack=b'{"saved":true}\n')
        self.assertEqual(body, 'fresh transcript'.encode())
        self.assertEqual((header['complete'], header['text_bytes']), (True, len(body)))
        self.assertEqual(header['sha256'], hashlib.sha256(self.audio).hexdigest())
        self.assertNotEqual(self.attempts[0], stale)
        self.assertFalse(jobdir.exists())
        self.assertEqual(list(self.progress.iterdir()), [])

    def test_unacknowledged_result_keeps_the_resumable_journal(self):
        with self.fake_decode(lambda target: target.write_text('fresh transcript')):
            header, _ = self.request(self.job)
        self.assertTrue(header['complete'])
        jobdir = self.output / self.ident
        self.assertTrue((jobdir / 'media-vault' / 'journal.jsonl').exists())
        self.assertEqual([p.name for p in jobdir.iterdir()], ['media-vault'])

    def test_linked_or_special_result_is_never_read(self):
        def link(target):
            target.symlink_to(self.canary)
        def hard(target):
            os.link(self.canary, target)
        for planted in (link, hard):
            with self.subTest(kind=planted.__name__), self.fake_decode(planted):
                header, body = self.request(self.job)
                self.assertEqual(header, {'complete': False, 'error': 'processing_failed'})
                self.assertIsNone(body)
        self.assertEqual(self.canary.read_text(), 'private text of another recording')

    def test_delete_removes_folder_and_progress_even_without_a_decode(self):
        jobdir = self.output / self.ident
        (jobdir / 'media-vault').mkdir(parents=True)
        (jobdir / 'media-vault' / 'segments.jsonl').write_text('{}')
        (self.progress / (self.ident + '.json')).write_text('{}')
        header, _ = self.request({'action': 'delete', 'id': self.ident})
        self.assertEqual(header, {'deleted': True})
        self.assertFalse(jobdir.exists())
        self.assertEqual(list(self.progress.iterdir()), [])
        self.assertEqual(self.request({'action': 'delete', 'id': '../' + self.ident})[0], None)

    def test_delete_aborts_the_running_decode_of_that_recording(self):
        started = threading.Event()
        def decode(workdir, src, target, lang, ident, env, alive=None, statedir=None):
            started.set()
            while True:
                alive()
                time.sleep(0.02)
        mine, theirs = socket.socketpair()
        with patch.object(asr, 'decode', decode):
            thread = threading.Thread(target=asr.handle, args=(theirs,))
            thread.start()
            mine.sendall(json.dumps(self.job).encode() + b'\n')
            self.assertTrue(started.wait(5))
            header, _ = self.request({'action': 'delete', 'id': self.ident})
            thread.join(5)
        mine.close()
        self.assertEqual(header, {'deleted': True})
        self.assertFalse(thread.is_alive())
        self.assertFalse((self.output / self.ident).exists())


if __name__ == '__main__':
    unittest.main()
