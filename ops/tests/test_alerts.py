from contextlib import redirect_stdout
import io
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import psy_alerts as alerts


class OwnerAlerts(unittest.TestCase):
    def test_memory_alert_requires_sustained_pressure_and_clears(self):
        state = {}
        for expected in (False, False, True):
            self.assertEqual(alerts.memory_status(state, 80 * 1024**2, 0), expected)
        self.assertFalse(alerts.memory_status(state, 500 * 1024**2, 0))
        self.assertEqual(state["memory_fails"], 0)

    def test_duplicate_suppression_retry_and_recovery_after_clean_runs(self):
        state = {}
        sender = Mock(side_effect=[alerts.Later(90), None, None])
        alerts.update_alerts(state, {'fault': 'safe summary'}, 100, sender)
        alerts.update_alerts(state, {'fault': 'safe summary'}, 189, sender)
        self.assertEqual(sender.call_count, 1)
        alerts.update_alerts(state, {'fault': 'safe summary'}, 190, sender)
        alerts.update_alerts(state, {'fault': 'safe summary'}, 191, sender)
        self.assertEqual(sender.call_count, 2)
        # Hysteresis: two clean runs, a relapse, then three clean runs before "resolved".
        for now in (200, 260):
            alerts.update_alerts(state, {}, now, sender)
        alerts.update_alerts(state, {'fault': 'safe summary'}, 320, sender)
        for now in (380, 440):
            alerts.update_alerts(state, {}, now, sender)
        self.assertEqual(sender.call_count, 2)
        alerts.update_alerts(state, {}, 500, sender)
        self.assertEqual(sender.call_count, 3)
        self.assertIn('Больше не наблюдается', sender.call_args[0][0])
        self.assertIn('safe summary', sender.call_args[0][0])
        self.assertEqual(state['alerts'], {})

    def test_one_digest_per_run_and_growing_reminders(self):
        state, sender = {}, Mock()
        problems = {'a': 'first fault', 'b': 'second fault', 'c': 'third fault'}
        alerts.update_alerts(state, problems, 1000, sender)
        self.assertEqual(sender.call_count, 1)
        for summary in problems.values():
            self.assertIn(summary, sender.call_args[0][0])
        sent_at = []
        for now in range(1060, 1000 + 12 * 3600, 60):
            calls = sender.call_count
            alerts.update_alerts(state, problems, now, sender)
            if sender.call_count > calls:
                sent_at.append(now - 1000)
                self.assertIn('Все еще не устранено', sender.call_args[0][0])
        self.assertEqual(sent_at, [3600, 3600 + 3 * 3600, 3600 + 9 * 3600])

    def test_hourly_cap_holds_messages_for_the_next_digest(self):
        state, sender = {}, Mock()
        for minute in range(alerts.HOURLY_CAP):
            # A new fault every run, resolved never: each run would want to send.
            alerts.update_alerts(state, {f'f{i}': f'fault {i}' for i in range(minute + 1)}, minute * 60, sender)
        self.assertEqual(sender.call_count, alerts.HOURLY_CAP)
        with redirect_stdout(io.StringIO()) as out:
            alerts.update_alerts(state, {f'f{i}': f'fault {i}' for i in range(9)}, 600, sender)
            alerts.update_alerts(state, {f'f{i}': f'fault {i}' for i in range(9)}, 660, sender)
        self.assertEqual(sender.call_count, alerts.HOURLY_CAP)
        self.assertEqual(out.getvalue().count('alerts_held'), 1)
        alerts.update_alerts(state, {f'f{i}': f'fault {i}' for i in range(9)}, 3601, sender)
        self.assertEqual(sender.call_count, alerts.HOURLY_CAP + 1)
        for i in range(alerts.HOURLY_CAP, 9):
            self.assertIn(f'fault {i}', sender.call_args[0][0])

    def test_events_are_reported_once_even_when_held_and_never_resolved(self):
        state = {}
        failing = Mock(side_effect=alerts.Later(120, 'http=502'))
        alerts.update_alerts(state, {'restart:psy-asr': 'restarted'}, 100, failing)
        sender = Mock()
        alerts.update_alerts(state, {}, 160, sender)          # the event is gone, delivery waits
        sender.assert_not_called()
        alerts.update_alerts(state, {}, 230, sender)
        self.assertEqual(sender.call_count, 1)
        self.assertIn('restarted', sender.call_args[0][0])
        self.assertIn('http=502', sender.call_args[0][0])     # the owner learns about the failed attempt
        for now in (290, 350, 410, 470):
            alerts.update_alerts(state, {}, now, sender)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(state['alerts'], {})

    def test_state_from_previous_version_is_migrated(self):
        state = {'alerts': {'fault': {'due': 5000, 'summary': 'old', 'sent': True}}}
        sender = Mock()
        alerts.update_alerts(state, {'fault': 'old'}, 4000, sender)
        sender.assert_not_called()
        alerts.update_alerts(state, {'fault': 'old'}, 5000, sender)
        self.assertEqual(sender.call_count, 1)

    def test_delivery_failure_logs_http_status_without_credential(self):
        secret = '123456789:' + 'S' * 35
        error = urllib.error.HTTPError('https://api.telegram.org/bot' + secret + '/sendMessage', 401,
                                       'Unauthorized ' + secret, {}, io.BytesIO(b'{}'))
        env = {'PSY_OWNER_ID': '101', 'TELEGRAM_BOT_TOKEN': secret}
        state = {}
        with patch.dict(alerts.os.environ, env), patch.object(alerts.urllib.request, 'urlopen', side_effect=error), \
                redirect_stdout(io.StringIO()) as out:
            for now in (100, 400, 700):
                alerts.update_alerts(state, {'fault': 'summary'}, now, alerts.send_owner)
        self.assertNotIn(secret, out.getvalue())
        self.assertNotIn('api.telegram.org', out.getvalue())
        self.assertIn('http=401', out.getvalue())
        self.assertEqual(state['delivery']['failures'], 3)
        self.assertGreaterEqual(state['delivery']['failures'], alerts.FAILING_AFTER)

    def test_token_prefers_staged_credential_and_falls_back_to_profile(self):
        staged = '123456789:' + 'C' * 35
        profile = '123456789:' + 'P' * 35
        with tempfile.TemporaryDirectory() as folder:
            credentials = Path(folder) / 'credentials'
            credentials.mkdir()
            env_file = Path(folder) / '.env'
            env_file.write_text('OTHER=1\nTELEGRAM_BOT_TOKEN="' + profile + '"\n')
            env = {'CREDENTIALS_DIRECTORY': str(credentials), 'PSY_TELEGRAM_CREDENTIAL_FILE': str(env_file)}
            with patch.dict(alerts.os.environ, env):
                alerts.os.environ.pop('TELEGRAM_BOT_TOKEN', None)
                (credentials / 'telegram').write_text(staged + '\n')
                self.assertEqual(alerts.token(), staged)
                (credentials / 'telegram').write_text('absent')   # SetCredential= placeholder
                self.assertEqual(alerts.token(), profile)

    def test_fifo_planted_as_profile_env_does_not_block(self):
        with tempfile.TemporaryDirectory() as folder:
            fifo = Path(folder) / '.env'
            os.mkfifo(fifo)
            with patch.dict(alerts.os.environ, {'PSY_TELEGRAM_CREDENTIAL_FILE': str(fifo)}):
                alerts.os.environ.pop('TELEGRAM_BOT_TOKEN', None)
                alerts.os.environ.pop('CREDENTIALS_DIRECTORY', None)
                with self.assertRaises(OSError):
                    alerts.token()

    def test_timers_disabled_or_late_are_reported(self):
        now = 2_000_000_000
        def show(unit, *names):
            if unit == 'psy-backup.timer':
                return {'UnitFileState': 'disabled', 'ActiveState': 'inactive', 'LastTriggerUSec': ''}
            if unit == 'psy-healthcheck.timer':
                return {'UnitFileState': 'enabled', 'ActiveState': 'active',
                        'LastTriggerUSec': time.strftime('%a %Y-%m-%d %H:%M:%S UTC', time.gmtime(now - 3600))}
            if unit == 'psy-alerts.timer':
                return {'UnitFileState': 'enabled', 'ActiveState': 'active', 'LastTriggerUSec': 'n/a'}
            return {'UnitFileState': 'enabled', 'ActiveState': 'active',
                    'LastTriggerUSec': time.strftime('%a %Y-%m-%d %H:%M:%S UTC', time.gmtime(now - 30))}
        with patch.object(alerts, 'properties', side_effect=show):
            problems, enabled = alerts.timer_problems(now, booted_for=120)
            self.assertEqual(set(problems), {'timer:psy-backup.timer', 'timer:psy-healthcheck.timer'})
            self.assertNotIn('psy-backup.timer', enabled)
            problems, _ = alerts.timer_problems(now, booted_for=7200)   # never ran since a long-ago boot
            self.assertIn('timer:psy-alerts.timer', problems)

    def test_timer_enabled_after_boot_is_measured_from_its_start(self):
        now = 2_000_000_000
        stamp = lambda seconds_ago: time.strftime('%a %Y-%m-%d %H:%M:%S UTC', time.gmtime(now - seconds_ago))
        def show(unit, *names):
            info = {'UnitFileState': 'enabled', 'ActiveState': 'active', 'LastTriggerUSec': stamp(30)}
            if unit == 'psy-restart-outdated.timer':
                info.update(LastTriggerUSec='', ActiveEnterTimestamp=stamp(started))
            return info
        with patch.object(alerts, 'properties', side_effect=show):
            started = 120          # enabled by an update two minutes ago, first run tonight
            problems, _ = alerts.timer_problems(now, booted_for=300_000)
            self.assertNotIn('timer:psy-restart-outdated.timer', problems)
            started = 200_000      # enabled long ago and still never fired
            problems, _ = alerts.timer_problems(now, booted_for=300_000)
            self.assertIn('timer:psy-restart-outdated.timer', problems)

    def test_backup_age(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'last-success'
            with patch.object(alerts, 'BACKUP_SUCCESS', marker):
                self.assertIsNone(alerts.backup_problem(time.time(), timer_enabled=False))
                self.assertIn('еще не записана', alerts.backup_problem(time.time(), timer_enabled=True))
                marker.write_text('stamp\n')
                self.assertIsNone(alerts.backup_problem(time.time(), timer_enabled=True))
                old = time.time() - 27 * 3600
                os.utime(marker, (old, old))
                self.assertIn('27', alerts.backup_problem(time.time(), timer_enabled=True))

    def test_checkpointed_wal_database_read_from_a_read_only_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / 'group-psy'
            store.mkdir()
            path = store / 'group.sqlite3'
            db = sqlite3.connect(path)
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('CREATE TABLE requests(id INTEGER,state TEXT); CREATE TABLE job_progress(request_id INTEGER,updated REAL,stage TEXT);'
                             "INSERT INTO requests VALUES(1,'uncertain');")
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.close()
            for side in ('-wal', '-shm'):   # Linux removes them on close; some builds keep them
                Path(str(path) + side).unlink(missing_ok=True)
            store.chmod(0o500)
            try:
                self.assertEqual(len(alerts.group_status(path, 100)), 1)
                self.assertEqual(sorted(p.name for p in store.iterdir()), ['group.sqlite3'])
            finally:
                store.chmod(0o700)

    def test_failed_group_jobs_are_reported_for_the_operator(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'group.sqlite3'
            with sqlite3.connect(path) as db:
                db.executescript('CREATE TABLE requests(id INTEGER,state TEXT); CREATE TABLE job_progress(request_id INTEGER,updated REAL,stage TEXT);')
                db.executemany('INSERT INTO requests VALUES(?,?)', [(1,'failed'),(2,'failed'),(3,'queued')])
            messages = alerts.group_status(path, 100)
            self.assertEqual(len(messages), 1)
            self.assertIn('ошибки: 2', messages[0])
            self.assertIn('psy_group_admin.py', messages[0])

    def test_only_running_stale_jobs_alert_without_disclosing_content(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'group.sqlite3'
            with sqlite3.connect(path) as db:
                db.executescript('CREATE TABLE requests(id INTEGER,state TEXT); CREATE TABLE job_progress(request_id INTEGER,updated REAL,stage TEXT);')
                db.executemany('INSERT INTO requests VALUES(?,?)', [(1,'running'),(2,'queued'),(3,'sending')])
                db.executemany('INSERT INTO job_progress VALUES(?,?,?)',[(1,100,'model'),(2,1,'secret'),(3,1,'model')])
            self.assertEqual(alerts.group_status(path, 400), [])
            result = alerts.group_status(path, 521)
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0].endswith(': 1.'))
            self.assertNotIn('secret', str(result))

    def test_sender_rejects_group_address_before_loading_credential(self):
        with patch.dict(alerts.os.environ, {'PSY_OWNER_ID':'-202'}), patch.object(alerts, 'token') as token:
            with self.assertRaises(ValueError):
                alerts.send_owner('test')
            token.assert_not_called()

    def test_parked_model_retry_is_reported_even_when_no_job_is_stale(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'group.sqlite3'
            with sqlite3.connect(path) as db:
                db.executescript('CREATE TABLE requests(id INTEGER,state TEXT); CREATE TABLE job_progress(request_id INTEGER,updated REAL,stage TEXT); CREATE TABLE model_retries(request_id INTEGER,attempts INTEGER);')
                db.execute("INSERT INTO requests VALUES(1,'queued')")
                db.execute("INSERT INTO model_retries VALUES(1,1)")
            messages = alerts.group_status(path, 100)
            self.assertEqual(len(messages), 1)
            self.assertIn('1.', messages[0])

    def test_inactive_gateway_is_observed_without_gateway_import(self):
        props = lambda name, *_: {'ActiveState': 'failed' if name=='hermes-gateway' else 'active', 'Result':'success','NRestarts':'0'}
        with patch.object(alerts, 'properties', side_effect=props), patch.object(alerts, 'group_status', return_value=[]), patch.object(alerts.socket, 'create_connection', side_effect=OSError), patch.dict(alerts.os.environ, {'PSY_PUBLIC_URL':'https://example.invalid'}):
            problems, _ = alerts.collect({}, 100)
        self.assertIn('service:hermes-gateway', problems)

    def test_remote_outage_alert_waits_for_three_probes_and_recovers(self):
        state = {}
        with patch.dict(alerts.os.environ, {'PSY_PUBLIC_URL':'https://example.invalid'}), patch.object(alerts.urllib.request, 'urlopen', side_effect=OSError):
            self.assertEqual(alerts.remote_status(state), {})
            self.assertEqual(alerts.remote_status(state), {})
            self.assertIn('primary_https', alerts.remote_status(state))
        response = Mock(status=200)
        response.read.return_value = b'{"ok":true}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.dict(alerts.os.environ, {'PSY_PUBLIC_URL':'https://example.invalid'}), patch.object(alerts.urllib.request, 'urlopen', return_value=response):
            self.assertEqual(alerts.remote_status(state), {})
            self.assertEqual(state['remote_fails'], 0)
