"""Regression examples for the installed owner monitor. No real network, token or bot calls."""
import copy
import importlib.util
from pathlib import Path
import sys
import os
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('psy_alerts_release', ROOT / 'ops/libexec/psy_alerts.py')
a = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, {'PSY_COMMON_PATH': str(ROOT / 'common/psy_common.py')}):
    spec.loader.exec_module(a)
KEY = 'service:hermes-gateway'


def fault_state(now=1000):
    return {'alerts': {KEY: {'notified': True, 'remind_at': now + 3600,
                            'reminders': 0, 'clean': 0, 'summary': 'Gateway unavailable'}},
            'sent': [now - 40, now - 30, now - 20, now - 10]}


def observe(state, active_state, now, *, result='success', changed=None, ready=True):
    info = {'ActiveState': active_state, 'Result': result, 'NRestarts': '0', 'MainPID': '321'}
    if changed is not None:
        info['StateChangeTimestampMonotonic'] = str(int(changed * 1000000))
    def properties(unit, *names):
        if unit == 'hermes-gateway':
            return info
        return {'ActiveState': 'active', 'Result': 'success', 'NRestarts': '0'}
    with patch.object(a, 'properties', side_effect=properties), \
         patch.object(a, 'timer_problems', return_value=({}, set())), \
         patch.object(a, 'backup_problem', return_value=None), \
         patch.object(a, 'group_status', return_value=[]), \
         patch.object(a, 'gateway_ready', return_value=ready), \
         patch.object(a.socket, 'create_connection', side_effect=OSError), \
         patch.dict(a.os.environ, {'PSY_PUBLIC_URL': 'https://example.invalid'}):
        problems, _ = a.collect(state, now)
    # TLS is intentionally mocked offline, and is irrelevant to these service examples.
    problems.pop('tls', None)
    return problems


class AlertReleaseRegression(unittest.TestCase):
    def test_normal_70_second_restart_never_emits_failure_or_recovery(self):
        state = {}
        sender = Mock()
        with patch.object(a.time, 'monotonic', return_value=2000):
            for now in (1000, 1006, 1070):
                problems = observe(state, 'deactivating', now, result='timeout')
                self.assertNotIn(KEY, problems)
                self.assertNotIn('failure:hermes-gateway', problems)
                a.update_alerts(state, problems, now, sender)
            problems = observe(state, 'active', 1088)
            a.update_alerts(state, problems, 1088, sender)
        sender.assert_not_called()

    def test_starting_and_reloading_have_bounded_grace(self):
        for active in ('activating', 'deactivating', 'reloading'):
            with self.subTest(state=active), patch.object(a.time, 'monotonic', return_value=2000):
                state = {}
                self.assertNotIn(KEY, observe(state, active, 1000))
                self.assertNotIn(KEY, observe(state, active, 1119))
                self.assertIn(KEY, observe(state, active, 1120))

    def test_stuck_transition_is_detected_on_first_probe_from_systemd_timestamp(self):
        with patch.object(a.time, 'monotonic', return_value=2000):
            problems = observe({}, 'deactivating', 1000, changed=1870)
        self.assertIn(KEY, problems)
        self.assertIn('более 2 минут', problems[KEY])

    def test_new_systemd_transition_clock_overrides_old_persisted_fallback(self):
        state = {'service_transitions': {'hermes-gateway': {'state': 'activating', 'since': 1}}}
        with patch.object(a.time, 'monotonic', return_value=2000):
            problems = observe(state, 'activating', 10000, changed=1995)
        self.assertNotIn(KEY, problems)

    def test_fallback_transition_clock_survives_monitor_restart(self):
        state = {}
        observe(state, 'activating', 1000)
        reloaded_state = copy.deepcopy(state)
        self.assertIn(KEY, observe(reloaded_state, 'activating', 1121))

    def test_inactive_gap_with_queued_restart_job_is_not_a_stopped_service(self):
        state = {}
        with patch.object(a.time, 'monotonic', return_value=2000):
            summary, grace = a.service_problem('hermes-gateway',
                {'ActiveState': 'inactive', 'Job': '744 /org/freedesktop/systemd1/job/744',
                 'StateChangeTimestampMonotonic': '1999000000'}, state, 1000)
        self.assertIsNone(summary)
        self.assertTrue(grace)
        summary, grace = a.service_problem('hermes-gateway',
            {'ActiveState': 'inactive', 'Job': '0'}, state, 1001)
        self.assertIsNotNone(summary)
        self.assertFalse(grace)

    def test_failed_and_inactive_do_not_get_restart_grace(self):
        for active in ('failed', 'inactive'):
            with self.subTest(state=active):
                problems = observe({}, active, 1000, result='exit-code')
                self.assertIn(KEY, problems)
                self.assertIn('failure:hermes-gateway', problems)

    def test_prior_notified_fault_stays_open_through_restart(self):
        state = fault_state()
        sender = Mock()
        for now in (1000, 1030, 1060, 1090):
            problems = observe(state, 'activating', now)
            self.assertIn(KEY, problems)
            a.update_alerts(state, problems, now, sender)
        self.assertFalse(state['alerts'][KEY].get('resolved', False))
        sender.assert_not_called()

    def test_active_without_telegram_readiness_cannot_resolve_fault(self):
        state = fault_state()
        sender = Mock()
        for now in (1000, 1060, 1120, 1180):
            problems = observe(state, 'active', now, ready=False)
            self.assertIn(KEY, problems)
            a.update_alerts(state, problems, now, sender)
        sender.assert_not_called()
        self.assertFalse(state['alerts'][KEY].get('resolved', False))

    def test_first_confirmed_recovery_bypasses_full_hour_budget(self):
        state = fault_state()
        original_sent = list(state['sent'])
        sender = Mock()
        problems = observe(state, 'active', 1000, ready=True)
        self.assertEqual(problems, {})
        a.update_alerts(state, problems, 1000, sender)
        sender.assert_called_once()
        self.assertIn('ПСИ снова подключен к Telegram.', sender.call_args.args[0])
        self.assertNotIn(KEY, state['alerts'])
        self.assertEqual(state['sent'], original_sent)
        a.update_alerts(state, {}, 1001, sender)
        self.assertEqual(sender.call_count, 1)

    def test_recovery_does_not_piggyback_new_alerts_past_full_budget(self):
        state = fault_state()
        sender = Mock()
        problems = observe(state, 'active', 1000)
        problems['memory'] = 'Memory fault'
        a.update_alerts(state, problems, 1000, sender)
        sender.assert_called_once()
        self.assertNotIn('Memory fault', sender.call_args.args[0])
        self.assertFalse(state['alerts']['memory']['notified'])
        a.update_alerts(state, {'memory': 'Memory fault'}, 1001, sender)
        self.assertEqual(sender.call_count, 1)

    def test_recovery_respects_telegram_retry_after_and_has_no_duplicate(self):
        state = fault_state()
        sender = Mock(side_effect=[a.Later(90, 'http=429'), None])
        problems = observe(state, 'active', 1000)
        a.update_alerts(state, problems, 1000, sender)
        self.assertIn(KEY, state['alerts'])
        a.update_alerts(state, {}, 1089, sender)
        self.assertEqual(sender.call_count, 1)
        a.update_alerts(state, {}, 1090, sender)
        self.assertEqual(sender.call_count, 2)
        self.assertNotIn(KEY, state['alerts'])
        a.update_alerts(state, {}, 1091, sender)
        self.assertEqual(sender.call_count, 2)

    def test_new_fault_after_failed_recovery_send_never_sends_false_recovery(self):
        state = fault_state()
        sender = Mock(side_effect=a.Later(90, 'network'))
        a.update_alerts(state, observe(state, 'active', 1000), 1000, sender)
        self.assertTrue(state['alerts'][KEY]['resolved'])
        a.update_alerts(state, observe(state, 'failed', 1090), 1090, sender)
        self.assertNotIn('resolved', state['alerts'][KEY])
        self.assertEqual(sender.call_count, 1)

    def test_generic_recovery_keeps_three_clean_checks_but_not_hour_cap(self):
        state = fault_state()
        state['alerts']['tls'] = state['alerts'].pop(KEY)
        state['alerts']['tls']['summary'] = 'TLS fault'
        sender = Mock()
        for now in (1000, 1060):
            a.update_alerts(state, {}, now, sender)
        sender.assert_not_called()
        a.update_alerts(state, {}, 1120, sender)
        sender.assert_called_once()
        self.assertEqual(state['alerts'], {})

    def test_new_fault_and_reminders_still_respect_hour_cap(self):
        state = {'sent': [960, 970, 980, 990]}
        sender = Mock()
        a.update_alerts(state, {'memory': 'Memory fault'}, 1000, sender)
        sender.assert_not_called()
        self.assertFalse(state['alerts']['memory']['notified'])
        a.update_alerts(state, {'memory': 'Memory fault'}, 4561, sender)
        sender.assert_called_once()
        self.assertTrue(state['alerts']['memory']['notified'])

    def test_gateway_readiness_requires_responding_pid_and_connected_transport(self):
        def response(pid, state):
            import json
            payload = json.dumps({'ok': True, 'result': {'answering_pid': pid,
                     'platforms': {'telegram': {'state': state}}}}).encode() + b'\n'
            connection = Mock()
            connection.__enter__ = Mock(return_value=connection)
            connection.__exit__ = Mock(return_value=False)
            connection.recv.return_value = payload
            return connection
        for pid, transport, expected in ((321, 'connected', True), (320, 'connected', False),
                                        (321, 'connecting', False)):
            with self.subTest(pid=pid, transport=transport), patch.object(a.socket, 'socket', return_value=response(pid, transport)):
                self.assertEqual(a.gateway_ready('321'), expected)

    def test_readiness_failure_does_not_crash_monitor(self):
        with patch.object(a.socket, 'socket', side_effect=OSError):
            self.assertFalse(a.gateway_ready('321'))
        self.assertFalse(a.gateway_ready('0'))


if __name__ == '__main__':
    unittest.main()
