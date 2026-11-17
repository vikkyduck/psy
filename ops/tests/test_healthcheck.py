from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import psy_healthcheck as health


class GatewayHealth(unittest.TestCase):
    def test_recovery_decisions(self):
        with redirect_stdout(io.StringIO()):
            health.self_test()

    def test_stale_owner_alerts_fail_the_check_after_boot_grace(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / 'state.json'
            with patch.object(health, 'ALERTS', state), redirect_stdout(io.StringIO()) as out:
                self.assertTrue(health.alerts_watch(1000, booted_for=60))      # just booted
                self.assertFalse(health.alerts_watch(1000, booted_for=7200))   # never ran
                state.write_text(json.dumps({'checked_at': 900}))
                self.assertTrue(health.alerts_watch(1000, booted_for=7200))
                self.assertFalse(health.alerts_watch(1601, booted_for=7200))
                state.write_text(json.dumps({'checked_at': 1000, 'delivery': {'failures': 4, 'reason': 'http=401'}}))
                self.assertTrue(health.alerts_watch(1000, booted_for=7200))   # running, only reported
            self.assertIn('http=401', out.getvalue())

    def test_fifo_or_link_planted_in_the_profile_never_blocks_the_root_check(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / 'logs').mkdir()
            os.mkfifo(home / 'logs/gateway.log')
            os.mkfifo(home / '.env')
            with patch.object(health, 'HOME', home):
                with self.assertRaises(OSError):
                    health.last_ingress()
                with self.assertRaises(OSError):
                    health.telegram_pending()
            (home / 'logs/gateway.log').unlink()
            (home / 'target.log').write_text('2026-01-01 00:00:00,000 inbound message: platform=telegram\n')
            (home / 'logs/gateway.log').symlink_to(home / 'target.log')
            with patch.object(health, 'HOME', home), self.assertRaises(OSError):
                health.last_ingress()
            (home / 'logs/gateway.log').unlink()
            (home / 'logs/gateway.log').write_text('x' * 300000 + '\n2026-01-01 00:00:01,000 inbound message: platform=telegram\n')
            with patch.object(health, 'HOME', home):
                self.assertEqual(health.last_ingress(), '2026-01-01 00:00:01,000')


if __name__ == '__main__':
    unittest.main()
