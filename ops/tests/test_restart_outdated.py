from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import psy_restart_outdated as nightly

NEEDRESTART = """NEEDRESTART-VER: 3.6
NEEDRESTART-KSTA: 1
NEEDRESTART-SVC: dbus.service
NEEDRESTART-SVC: hermes-gateway.service
NEEDRESTART-SVC: psy-asr.service
"""


class Result:
    def __init__(self, returncode=0, stdout=''):
        self.returncode, self.stdout = returncode, stdout


class NightlyRestart(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.health = root / 'state.json'
        self.log = root / 'gateway.log'
        self.write_queue(queued=0, processing=0)
        self.restarted, self.group_count, self.restart_fails = [], 0, False
        self.upload_count, self.agent_busy = 0, False
        self.root = root
        self.patches = [patch.object(nightly, 'UPLOAD_HEALTH', self.health), patch.object(nightly, 'GATEWAY_LOG', self.log),
                        patch.object(nightly, 'GROUP_DB', str(root / 'group.sqlite3')),
                        patch.object(nightly, 'UPLOAD_DB', str(root / 'missing-uploads.sqlite3')),
                        patch.object(nightly, 'STATE', root / 'restart-state.json'),
                        patch.object(nightly, 'owner_agent_busy', side_effect=lambda: self.agent_busy),
                        patch.object(nightly, 'run', side_effect=self.fake_run), patch.object(nightly.time, 'sleep')]
        (root / 'group.sqlite3').write_bytes(b'')
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.folder.cleanup()

    def write_queue(self, queued, processing, age=30):
        self.health.write_text(json.dumps({'checked_at': time.time() - age, 'queue': {'queued': queued, 'processing': processing}}))

    def fake_run(self, *command, timeout=60):
        if command[0] == 'needrestart':
            return Result(0, NEEDRESTART)
        if command[0] == 'runuser':
            return Result(0, f'{self.upload_count if nightly.UPLOAD_PROBE in command else self.group_count}\n')
        if command[:2] == ('systemctl', 'restart'):
            self.restarted.append(command[2])
            return Result(1 if self.restart_fails else 0)
        if command[:2] == ('systemctl', 'is-active'):
            return Result(1 if self.restart_fails else 0)
        raise AssertionError(command)

    def main(self, *args):
        with patch('sys.argv', ['x', *args]), redirect_stdout(io.StringIO()) as out:
            code = nightly.main()
        return code, out.getvalue()

    def test_only_outdated_psy_services_restart_in_order_when_idle(self):
        self.log.write_text('2000-01-01 00:00:00,000 INFO inbound message: platform=telegram\n')
        code, out = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(self.restarted, ['psy-asr.service', 'hermes-gateway.service'])
        self.assertIn('restarted psy-asr.service, hermes-gateway.service', out)

    def test_check_only_never_restarts(self):
        code, _ = self.main('--check-only')
        self.assertEqual((code, self.restarted), (0, []))

    def test_busy_group_upload_or_recent_message_waits(self):
        self.group_count = 2
        self.assertIn('group queue has 2', self.main()[1])
        self.group_count = 0
        self.write_queue(queued=0, processing=1)
        self.assertIn('upload queue has 1', self.main()[1])
        self.write_queue(queued=0, processing=0, age=3600)
        self.assertIn('upload queue unknown', self.main()[1])
        self.write_queue(queued=0, processing=0)
        recent = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - 60))
        self.log.write_text(recent + ',000 INFO inbound message: platform=telegram chat=x\n')
        self.assertIn('Telegram message arrived', self.main()[1])
        self.assertEqual(self.restarted, [])

    def test_fifo_planted_as_log_does_not_hang(self):
        os.mkfifo(self.log)
        code, _ = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(self.restarted, ['psy-asr.service', 'hermes-gateway.service'])

    def test_failed_restart_fails_the_run(self):
        self.restart_fails = True
        code, out = self.main()
        self.assertEqual(code, 1)
        self.assertIn('not running after restart', out)

    def test_nothing_outdated(self):
        with patch.object(nightly, 'outdated', return_value=[]):
            self.assertEqual(self.main(), (0, 'restart-outdated: no PSY service runs outdated code\n'))

    def test_upload_database_and_owner_agent_decide_when_available(self):
        (self.root / 'uploads.sqlite3').write_bytes(b'')
        with patch.object(nightly, 'UPLOAD_DB', str(self.root / 'uploads.sqlite3')):
            self.write_queue(queued=5, processing=0)      # the snapshot would say busy; the database decides
            self.assertEqual(self.main()[0], 0)
            self.assertEqual(self.restarted, ['psy-asr.service', 'hermes-gateway.service'])
            self.restarted.clear()
            self.upload_count = 1
            self.assertIn('upload queue has 1', self.main()[1])
        self.upload_count, self.agent_busy = 0, True
        self.write_queue(queued=0, processing=0)
        self.assertIn('running an agent turn', self.main()[1])
        self.assertEqual(self.restarted, [])

    def test_outdated_for_two_days_without_an_idle_night_alerts(self):
        self.group_count = 1
        self.assertEqual(self.main()[0], 0)
        state = json.loads((self.root / 'restart-state.json').read_text())
        (self.root / 'restart-state.json').write_text(json.dumps({'since': state['since'] - 49 * 3600}))
        code, out = self.main()
        self.assertEqual(code, 1)
        self.assertIn('never idle', out)
        self.group_count = 0
        self.assertEqual(self.main()[0], 0)
        self.assertEqual(json.loads((self.root / 'restart-state.json').read_text())['since'], None)


class Probes(unittest.TestCase):
    """The SQL the nightly helper runs as hermes, against real SQLite files."""

    def probe(self, code, database):
        import subprocess, sys
        return int(subprocess.run([sys.executable, '-I', '-c', code, str(database)], capture_output=True,
                                  text=True, check=True).stdout)

    def test_parked_group_requests_do_not_keep_the_bot_busy(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'group.sqlite3'
            db = sqlite3.connect(path)
            db.executescript("""CREATE TABLE requests(id TEXT, state TEXT);
                CREATE TABLE model_retries(request_id TEXT, retry_at REAL);
                CREATE TABLE delivery_parts(request_id TEXT, state TEXT, retry_at REAL);""")
            later = time.time() + 3600
            db.executemany('INSERT INTO requests VALUES(?,?)', [('oauth', 'queued'), ('flood', 'queued'), ('done', 'uncertain')])
            db.execute('INSERT INTO model_retries VALUES(?,?)', ('oauth', later))
            db.execute('INSERT INTO delivery_parts VALUES(?,?,?)', ('flood', 'retry', later))
            db.commit()
            self.assertEqual(self.probe(nightly.GROUP_PROBE, path), 0)
            db.executemany('INSERT INTO requests VALUES(?,?)', [('ready', 'queued'), ('live', 'running')])
            db.commit()
            self.assertEqual(self.probe(nightly.GROUP_PROBE, path), 2)

    def test_uploads_waiting_for_a_retry_time_do_not_count(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sqlite3'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE jobs(id TEXT, state TEXT, next_try INTEGER)')
            db.executemany('INSERT INTO jobs VALUES(?,?,?)', [('a', 'queued', int(time.time()) + 600),
                                                             ('b', 'delivered', 0), ('c', 'uploading', 0)])
            db.commit()
            self.assertEqual(self.probe(nightly.UPLOAD_PROBE, path), 0)
            db.executemany('INSERT INTO jobs VALUES(?,?,?)', [('d', 'queued', 0), ('e', 'processing', 0)])
            db.commit()
            self.assertEqual(self.probe(nightly.UPLOAD_PROBE, path), 2)


if __name__ == '__main__':
    unittest.main()
