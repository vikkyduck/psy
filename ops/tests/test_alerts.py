import sqlite3
import tempfile
import unittest
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

    def test_duplicate_suppression_retry_and_recovery(self):
        state = {}
        sender = Mock(side_effect=[alerts.Later(90), None, None])
        alerts.update_alerts(state, {'fault': 'safe summary'}, 100, sender)
        alerts.update_alerts(state, {'fault': 'safe summary'}, 189, sender)
        self.assertEqual(sender.call_count, 1)
        alerts.update_alerts(state, {'fault': 'safe summary'}, 190, sender)
        alerts.update_alerts(state, {'fault': 'safe summary'}, 191, sender)
        self.assertEqual(sender.call_count, 2)
        alerts.update_alerts(state, {}, 200, sender)
        self.assertEqual(sender.call_count, 3)
        self.assertEqual(state['alerts'], {})

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

    def test_inactive_gateway_is_observed_without_gateway_import(self):
        props = lambda name: {'ActiveState': 'failed' if name=='hermes-gateway' else 'active', 'Result':'success','NRestarts':'0'}
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
