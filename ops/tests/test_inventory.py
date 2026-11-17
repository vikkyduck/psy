"""ops/inventory.json is the one unit list; everything else must agree with it."""
import importlib.util
import json
from pathlib import Path
import re
import unittest

import psy_alerts

OPS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('inventory', OPS / 'inventory.py')
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)
UNITS = inventory.UNITS


def bare(name):
    return name.rsplit('.', 1)[0]


class Inventory(unittest.TestCase):
    def test_every_unit_file_is_listed_and_every_listed_unit_exists(self):
        files = {p.name for p in (OPS / 'systemd').iterdir() if p.is_file()}
        listed = set(UNITS) | {u['timer'] for u in UNITS.values() if u.get('timer')}
        self.assertEqual(files, listed)

    def test_timers_start_their_service(self):
        for name, unit in UNITS.items():
            if not unit.get('timer'):
                continue
            text = (OPS / 'systemd' / unit['timer']).read_text()
            target = re.search(r'^Unit=(.+)$', text, re.M)
            self.assertEqual(target.group(1) if target else bare(unit['timer']) + '.service', name)

    def test_alerted_checks_have_timers_and_gaps(self):
        for name, unit in UNITS.items():
            if unit['kind'] == 'check' and unit['installer'] != 'none':
                self.assertIn('timer', unit, name)
                self.assertGreater(unit.get('max_gap', 0), 0, name)

    def test_alert_wiring(self):
        template = inventory.INVENTORY['on_failure'].replace('%n', '')
        self.assertEqual(UNITS[template]['kind'], 'alert-instance')
        for name in UNITS:
            text = (OPS / 'systemd' / name).read_text()
            # OnFailure= comes from the drop-in only, so one unit cannot alert twice; never on the alert units.
            self.assertNotIn('\nOnFailure=', text, name)
        self.assertFalse(UNITS['psy-alerts.service'].get('alert'))

    def test_psy_alerts_watches_exactly_the_inventory(self):
        daemons = {bare(n) for n, u in UNITS.items() if u['kind'] == 'daemon'}
        checks = {bare(n) for n, u in UNITS.items() if u.get('alert') and u['kind'] == 'check'}
        timers = {u['timer']: u['max_gap'] for u in UNITS.values() if u.get('max_gap')}
        self.assertEqual(set(psy_alerts.CORE), daemons)
        self.assertEqual(set(psy_alerts.CHECKS), checks)
        self.assertEqual(psy_alerts.TIMERS, timers)

    def test_restart_order_covers_the_daemons(self):
        self.assertEqual(sorted(inventory.INVENTORY['restart_order']),
                         sorted(n for n, u in UNITS.items() if u['kind'] == 'daemon'))
        self.assertEqual(inventory.INVENTORY['restart_order'][-1], 'hermes-gateway.service')

    def test_queries(self):
        idle = inventory.query('idle')
        self.assertIn('psy-backup.timer', idle)
        self.assertIn('psy-member.socket', idle)
        self.assertNotIn('psy-audio-priority.timer', idle)
        self.assertEqual(set(inventory.query('unit-files', 'backup')), {'psy-backup.service', 'psy-backup.timer'})
        runtime = set(inventory.query('unit-files', 'runtime'))
        self.assertTrue(runtime.isdisjoint(inventory.query('unit-files', 'backup')))
        self.assertIn('psy-alert@.service', runtime)
        self.assertNotIn('psy-audio-priority.service', runtime)
        with self.assertRaises(SystemExit):
            inventory.query('unknown')

    def test_installers_and_checks_read_the_inventory(self):
        for script in ('prepare-host', 'install-runtime', 'install-backup', 'test-host', 'restore-permissions'):
            text = (OPS / script).read_text()
            self.assertIn('inventory.py', text, script)
            self.assertNotRegex(text, r'psy-(healthcheck|alerts|vault-archive)\.timer', script)


if __name__ == '__main__':
    unittest.main()
