import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

# Installed scripts load /usr/local/lib/psy-common/psy_common.py; tests use the repository copy.
os.environ.setdefault('PSY_COMMON_PATH', str(Path(__file__).resolve().parents[2] / 'common/psy_common.py'))
import upload_healthcheck as health


class UploadRecovery(unittest.TestCase):
    def run_checks(self, state, start, results, public=True):
        restarts = []
        for i, ok in enumerate(results):
            units = health.decide(state, start + 60 * i, ok, True, True, public)
            if units:
                restarts.append((60 * i, units))
                state.update(last_restart=start + 60 * i, fails=0)
        return restarts

    def test_public_origin_failures_alert_but_never_restart(self):
        state = {'queue_status': 'ok'}
        self.assertEqual(self.run_checks(state, 10_000, [True] * 10, public=False), [])
        self.assertFalse(state['healthy'])
        self.assertEqual(state['public_fails'], 10)
        health.decide(state, 20_000, True, True, True, True)
        self.assertTrue(state['healthy'])

    def test_local_failures_restart_only_the_failing_unit_with_cooldown(self):
        state = {'queue_status': 'ok'}
        restarts = self.run_checks(state, 10_000, [False] * 20)
        self.assertEqual(restarts, [(120, ['psy-upload']), (1020, ['psy-upload'])])
        units = health.decide({'fails': 2, 'queue_status': 'ok'}, 50_000, True, False, True, True)
        self.assertEqual(units, ['psy-upload-worker'])

    def test_failed_restarts_back_off(self):
        state = {'queue_status': 'ok', 'restart_failures': 2}
        restarts = self.run_checks(state, 10_000, [False] * 120)
        self.assertEqual([t for t, _ in restarts], [120, 120 + 3600])

    def test_state_is_saved_before_the_restart_and_failure_recorded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'state.json').write_text(json.dumps({'fails': 5, 'queue_status': 'ok'}))
            seen = []

            def fake_run(command, **_):
                if command[:2] == ['systemctl', 'restart']:
                    seen.append(json.loads((root / 'state.json').read_text()))
                    return type('R', (), {'returncode': 1})()
                return type('R', (), {'returncode': 0})()

            with patch.object(health, 'ROOT', root), patch.object(health, 'local_ok', return_value=False), \
                    patch.object(health, 'public_ok', return_value=True), patch.object(health, 'queue_report'), \
                    patch.object(health.subprocess, 'run', side_effect=fake_run), patch('sys.argv', ['x']):
                self.assertEqual(health.main(), 1)
            self.assertEqual(seen[0]['restart_units'], ['psy-upload'])
            self.assertEqual(seen[0]['fails'], 0)
            final = json.loads((root / 'state.json').read_text())
            self.assertEqual(final['restart_failures'], 1)
            self.assertEqual(health.decide(final, final['last_restart'] + 1000, False, True, True, True), [])


if __name__ == '__main__':
    unittest.main()
