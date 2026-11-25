"""Fast standard-library tests. No service, model, credentials or network is used."""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import generate_reference
import group_example
import run_on_host


class FixtureDestinationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='psy-fixture-guards-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

    def test_new_direct_wav_in_private_temp_is_allowed(self):
        self.assertEqual(generate_reference.validate_destination(self.root, self.root / 'synthetic.wav'),
                         (self.root, self.root / 'synthetic.wav'))

    def test_production_directories_are_rejected(self):
        for root in ('/home/hermes/.hermes', '/opt/psy-upload', '/etc', '/var/lib', '/'):
            with self.subTest(root=root), self.assertRaises(ValueError):
                generate_reference.validate_destination(Path(root), Path(root) / 'synthetic.wav')

    def test_temp_directory_itself_is_not_a_fixture_scope(self):
        root = Path(tempfile.gettempdir()).resolve()
        with self.assertRaises(ValueError):
            generate_reference.validate_destination(root, root / 'synthetic.wav')

    def test_inherited_tmpdir_cannot_allow_a_production_directory(self):
        root = Path('/home/hermes/.hermes/fixture')
        with patch.object(tempfile, 'gettempdir', return_value='/home/hermes/.hermes'):
            with self.assertRaises(ValueError):
                generate_reference.validate_destination(root, root / 'synthetic.wav')

    def test_sibling_and_relative_outputs_are_rejected(self):
        for target in (self.root.parent / 'synthetic.wav', Path('synthetic.wav'), self.root / 'nested/audio.wav'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                generate_reference.validate_destination(self.root, target)

    def test_existing_file_and_symlink_are_never_overwritten(self):
        existing = self.root / 'existing.wav'
        existing.write_bytes(b'synthetic fixture')
        link = self.root / 'link.wav'
        link.symlink_to(existing)
        for target in (existing, link):
            with self.subTest(target=target), self.assertRaises(ValueError):
                generate_reference.validate_destination(self.root, target)
        self.assertEqual(existing.read_bytes(), b'synthetic fixture')

    def test_symbolic_fixture_directory_is_rejected(self):
        link = self.root / 'alias'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            generate_reference.validate_destination(link, link / 'synthetic.wav')

    def test_world_readable_fixture_directory_is_rejected(self):
        self.root.chmod(0o755)
        try:
            with self.assertRaises(ValueError):
                generate_reference.validate_destination(self.root, self.root / 'synthetic.wav')
        finally:
            self.root.chmod(0o700)

    def test_missing_tools_create_no_audio_or_directory(self):
        destination = self.root / 'unused'
        with patch.object(generate_reference, 'find_tools', side_effect=RuntimeError('not installed')):
            with self.assertRaises(RuntimeError):
                generate_reference.generate(destination, destination / 'synthetic.wav')
        self.assertFalse(destination.exists())

    def test_control_text_contains_only_synthetic_acceptance_phrase(self):
        phrase = generate_reference.REFERENCE.read_text(encoding='utf-8').casefold()
        for reference in ('synthetic test', 'green', 'train', 'london', 'nine', 'forty seven'):
            self.assertIn(reference, phrase)


class NetworkAndModeTests(unittest.TestCase):
    def test_stub_model_cannot_contact_real_codex(self):
        guard = group_example.NetworkGuard(43210, allow_model=False)
        guard.allowed_url('http://127.0.0.1:43210/bot-fixture')
        with self.assertRaises(group_example.IsolationViolation):
            guard.allowed_url('https://chatgpt.com/backend-api/codex/responses')
        self.assertEqual(guard.codex_http, 0)

    def test_real_mode_accepts_codex_but_rejects_real_telegram(self):
        guard = group_example.NetworkGuard(43210, allow_model=True)
        guard.allowed_url('https://chatgpt.com/backend-api/codex/responses')
        self.assertEqual(guard.codex_http, 1)
        for url in ('https://api.telegram.org', 'https://example.invalid', 'http://chatgpt.com',
                    'http://127.0.0.1:43211/bot-fixture'):
            with self.subTest(url=url), self.assertRaises(group_example.IsolationViolation):
                guard.allowed_url(url)

    def test_host_runner_starts_with_no_inherited_service_credentials(self):
        with patch.dict(os.environ, {'BOT_TOKEN': 'synthetic-value', 'CREDENTIALS_DIRECTORY': '/invalid-fixture'}):
            environment = run_on_host.clean_environment('/synthetic/home', '/synthetic/temp')
        self.assertEqual(set(environment), {'PATH', 'HOME', 'TMPDIR', 'LANG', 'PYTHONDONTWRITEBYTECODE'})
        self.assertEqual(environment['PYTHONDONTWRITEBYTECODE'], '1')


class BootstrapIsolationTests(unittest.TestCase):
    def test_only_entrypoint_maintenance_is_suppressed(self):
        names = ('restore_interrupted_pull', 'prepare_launch', 'recover_if_needed', 'activate_dependencies')
        originals = {name: Mock(side_effect=AssertionError('maintenance attempted')) for name in names}
        recovery = SimpleNamespace(restore_interrupted_pull=originals[names[0]], recover_if_needed=originals[names[2]])
        launch = SimpleNamespace(prepare_launch=originals[names[1]])
        environments = SimpleNamespace(activate_dependencies=originals[names[3]])
        transport = SimpleNamespace(enable_happy_eyeballs_on_client=lambda client: client)
        modules = {'hermes_cli._early_recovery': recovery, 'hermes_cli.venv_sync': launch,
                   'pm.environments': environments, 'agent.process_bootstrap': transport}
        def importing(name):
            if name == 'agent.process_bootstrap':
                self.assertEqual(os.environ['HERMES_DISABLE_LAZY_INSTALLS'], '1')
                for module, function in ((recovery, names[0]), (launch, names[1]),
                                         (recovery, names[2]), (environments, names[3])):
                    getattr(module, function)('synthetic')
            return modules[name]
        with patch.dict(sys.modules), patch.object(sys, 'path', ['/synthetic/dependencies']), \
                patch.dict(os.environ, {'HERMES_DISABLE_LAZY_INSTALLS': 'previous'}), \
                patch.object(group_example.importlib, 'import_module', side_effect=importing):
            sys.modules.pop('hermes_bootstrap', None)
            sys.modules.pop('agent.process_bootstrap', None)
            report = group_example.initialize_transport_without_startup(Path('/synthetic/dependencies'))
            self.assertEqual(report['suppressed_calls'], {name: 1 for name in names})
            self.assertEqual(os.environ['HERMES_DISABLE_LAZY_INSTALLS'], 'previous')
        self.assertIs(recovery.restore_interrupted_pull, originals[names[0]])
        self.assertIs(environments.activate_dependencies, originals[names[3]])
        for original in originals.values():
            original.assert_not_called()

    def test_preloaded_bootstrap_is_rejected(self):
        with patch.dict(sys.modules, {'hermes_bootstrap': SimpleNamespace()}):
            with self.assertRaises(group_example.IsolationViolation):
                group_example.initialize_transport_without_startup(Path('/synthetic/dependencies'))


class RuntimeSnapshotIsolationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.fixture = self.root / 'fixture'
        self.fixture.mkdir(mode=0o700)
        self.actual = self.root / 'actual-profile'
        self.actual.mkdir(mode=0o700)
        (self.actual / 'SOUL.md').write_text('SYNTHETIC OWNER PROFILE MUST NOT BE READ')
        (self.actual / 'config.yaml').write_text('SYNTHETIC PRIVATE CONFIG MUST NOT BE READ')
        self.stack.enter_context(patch.dict(os.environ, {'HERMES_HOME': str(self.actual)}))
        self.code = self.fixture / 'code'
        self.code.mkdir(mode=0o700)
        (self.code / 'psy_fixture.py').write_text('# Synthetic public code\n')
        modules = Path(__file__).resolve().parents[2] / 'gateway/plugins/platforms/telegram'
        def load(name):
            spec = importlib.util.spec_from_file_location('fixture_' + name, modules / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        self.observability = load('psy_group_observability')
        self.store = load('psy_group_store')
        self.stack.enter_context(patch.object(self.observability, '_environment', return_value=b'{"synthetic":true}'))
        original_read = self.observability._read
        def read(path):
            if Path(path).is_relative_to(self.actual):
                raise AssertionError('actual private profile accessed')
            return original_read(path)
        self.stack.enter_context(patch.object(self.observability, '_read', side_effect=read))
        original_database = self.store.database
        self.stack.enter_context(patch.object(self.store, 'database', lambda *args, **kwargs: original_database(self.fixture)))
        self.route = SimpleNamespace(capture_runtime=self.observability.capture_runtime)
        self.profile = group_example.fixture_runtime_capture(self.route, self.fixture,
            {'provider': 'openai-codex', 'default': 'fixture-model', 'api_key': 'DO_NOT_COPY'}, self.stack)

    def capture(self):
        return self.route.capture_runtime('fixture-model', code_dir=self.code, root=self.fixture / 'runtime-snapshots')

    def test_only_synthetic_profile_is_captured_without_rebinding_auth_home(self):
        snapshot = self.capture()
        root = self.fixture / 'runtime-snapshots' / snapshot['snapshot_id'] / 'profile'
        self.assertEqual((root / 'SOUL.md').read_text(), group_example.SYNTHETIC_SOUL)
        config = json.loads((root / 'config.yaml').read_text())
        self.assertEqual(config, {'model': {'provider': 'openai-codex', 'default': 'fixture-model'}})
        self.assertNotIn('DO_NOT_COPY', json.dumps(config))
        self.assertEqual(os.environ['HERMES_HOME'], str(self.actual))
        self.assertEqual(self.profile.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.profile / 'SOUL.md').stat().st_mode & 0o777, 0o600)

    def test_snapshot_wrapper_refuses_external_root_or_real_profile(self):
        for options in ({'root': self.actual}, {'profile': self.actual, 'root': self.fixture / 'runtime-snapshots'}):
            with self.subTest(options=options), self.assertRaises(group_example.IsolationViolation):
                self.route.capture_runtime('fixture-model', code_dir=self.code, **options)

    def finished_job(self, *, effort='max', task_status='complete'):
        runtime = self.capture()
        key = 'update:' + str(group_example.UPDATE)
        self.store.pending(group_example.MEMBER, group_example.UPDATE,
                           json.dumps({'message': {'message_id': group_example.INCOMING,
                                                   'from': {'id': group_example.MEMBER}}}))
        self.store.claim_next()
        generation = {'effort_counts': {effort: 3}, 'fallback_stages': 0,
                      'effort_policy': 'maximum_supported_no_downgrade', 'generation_complete': True}
        details = {'task_status': task_status, 'generation_complete': True, 'missing_requirements': [],
                   'quality_metadata': {'runtime': runtime, 'generation_audit': generation}}
        turn = self.store.remember(group_example.MEMBER, 0, 'synthetic arithmetic', '42', result_details=details)
        self.store.audit_event(key, 'result_saved', self.observability.result_audit(details), state='sending', turn_id=turn)
        self.store.confirm_result_delivery(group_example.MEMBER, 0, turn)
        self.store.audit_event(key, 'delivery_confirmed', {'task_status': task_status}, state='finished', turn_id=turn)
        with self.store.database() as database:
            database.execute("INSERT INTO delivery_parts VALUES(?,0,'synthetic','confirmed',9901,NULL)", (group_example.UPDATE,))
        self.store.settle(group_example.UPDATE, True)
        return turn

    def test_durable_audit_survives_cleanup_and_reports_verified_maximum_and_delivery(self):
        self.finished_job()
        report = group_example.durable_acceptance(self.store, self.fixture, self.profile, expected_effort='max')
        self.assertTrue(report['durable_diagnostics_after_cleanup'])
        self.assertEqual(report['durable_task_status'], 'complete')
        self.assertIs(report['durable_generation_complete'], True)
        self.assertEqual(report['generation_effort'], 'max')
        self.assertEqual(report['durable_delivery_status'], 'confirmed')
        self.assertTrue(report['runtime_snapshot_private'])
        self.assertTrue(report['synthetic_profile_only'])
        self.assertNotIn('SYNTHETIC OWNER PROFILE', json.dumps(report))

    def test_nonmaximum_generation_cannot_pass_acceptance(self):
        self.finished_job(effort='high')
        with self.assertRaises(AssertionError):
            group_example.durable_acceptance(self.store, self.fixture, self.profile, expected_effort='max')

    def test_partial_task_cannot_pass_acceptance(self):
        self.finished_job(task_status='partial')
        with self.assertRaises(AssertionError):
            group_example.durable_acceptance(self.store, self.fixture, self.profile, expected_effort='max')

    def test_corrupted_or_public_snapshot_cannot_pass_acceptance(self):
        self.finished_job()
        snapshot = next((self.fixture / 'runtime-snapshots').iterdir())
        (snapshot / 'profile' / 'SOUL.md').chmod(0o644)
        with self.assertRaises(AssertionError):
            group_example.durable_acceptance(self.store, self.fixture, self.profile, expected_effort='max')
        (snapshot / 'profile' / 'SOUL.md').chmod(0o600)
        (snapshot / 'profile' / 'SOUL.md').write_text('non-synthetic copied content')
        with self.assertRaises(AssertionError):
            group_example.durable_acceptance(self.store, self.fixture, self.profile, expected_effort='max')


if __name__ == '__main__':
    unittest.main()
