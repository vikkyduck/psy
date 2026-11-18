"""The shared helpers in common/psy_common.py, and the loader every consumer carries."""
import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
os.environ.setdefault('PSY_COMMON_PATH', str(REPO / 'common/psy_common.py'))
spec = importlib.util.spec_from_file_location('psy_common_under_test', REPO / 'common/psy_common.py')
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)

# Installed consumers load /usr/local/lib/psy-common/psy_common.py; release tools load the copy next to them.
INSTALLED_CONSUMERS = ('ops/libexec/psy_alerts.py', 'ops/libexec/psy_healthcheck.py', 'ops/libexec/psy_vault_archive.py',
                       'ops/libexec/upload_healthcheck.py', 'ops/libexec/psy_restart_outdated.py',
                       'ops/libexec/audio_priority.py', 'gateway/upload_broker.py', 'media/psy_upload_common.py')
RELEASE_TOOLS = ('ops/instance_env.py', 'ops/stage-observer.py', 'tools/telegram_entry.py', 'gateway/configure_profile.py')
ENV = {'PSY_OWNER_ID': '101', 'PSY_GROUP_ID': '-202', 'PSY_BOT_ID': '303',
       'PSY_BOT_USERNAME': 'fixture_bot', 'PSY_PUBLIC_URL': 'https://fixture.invalid:9443'}


class TelegramIds(unittest.TestCase):
    def test_only_telegram_spelling_is_accepted(self):
        self.assertEqual(common.telegram_id('101', 'PSY_OWNER_ID'), 101)
        self.assertEqual(common.telegram_id('-1001234567890', 'PSY_GROUP_ID', group=True), -1001234567890)
        self.assertEqual(common.telegram_id(str(2**52 - 1), 'PSY_BOT_ID'), 2**52 - 1)
        for raw in ('', '0', '0101', '+101', ' 101', '101 ', '101\n', '1e3', '١٠١', '-101', str(2**52), '9' * 17, None, 101):
            with self.subTest(raw=raw), self.assertRaises(ValueError) as caught:
                common.telegram_id(raw, 'PSY_OWNER_ID')
            self.assertEqual(str(caught.exception), 'Invalid PSY_OWNER_ID')
        for raw in ('202', '-0', '-0202', '--202', '-' + str(2**52), '- 202'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                common.telegram_id(raw, 'PSY_GROUP_ID', group=True)

    def test_bot_username_is_a_botfather_name_without_at(self):
        for good in ('a_bot', 'DemoTestBot', 'x' * 29 + 'BOT'):
            self.assertEqual(common.bot_username(good), good)
        for bad in ('abot', '@fixture_bot', '1_bot', 'fixture', 'x' * 30 + 'bot', 'fixture-bot', 'fixture_bot '):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                common.bot_username(bad)

    def test_public_url_is_one_https_origin(self):
        cases = {'https://fixture.invalid': 'https://fixture.invalid',
                 'https://fixture.invalid/': 'https://fixture.invalid',
                 'https://fixture.invalid:9443': 'https://fixture.invalid:9443',
                 'https://[2001:db8::1]:9443/': 'https://[2001:db8::1]:9443'}
        for raw, origin in cases.items():
            self.assertEqual(common.https_origin(raw), origin)
        for bad in ('http://fixture.invalid', 'https://', 'https://user@fixture.invalid', 'https://u:p@fixture.invalid',
                    'https://fixture.invalid/path', 'https://fixture.invalid//', 'https://fixture.invalid?',
                    'https://fixture.invalid#', 'https://fixture.invalid?a=1', 'https://fixture.invalid:0',
                    'https://fixture.invalid:65536', 'https://fixture.invalid:x', 'https://[::1', 'https://fixture .invalid',
                    'https://fixture.invalid\\', 'https://фикстура.invalid', 'https://fixture.invalid\t', ' https://fixture.invalid'):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as caught:
                common.https_origin(bad)
            self.assertEqual(str(caught.exception), 'PSY_PUBLIC_URL must be an HTTPS origin')

    def test_instance_checks_every_identity_value(self):
        settings = common.instance(ENV)
        self.assertEqual(tuple(settings), (101, -202, 303, 'fixture_bot', 'https://fixture.invalid:9443'))
        with self.assertRaisesRegex(ValueError, 'must differ'):
            common.instance({**ENV, 'PSY_BOT_ID': '101'})
        for key in ENV:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                common.instance({k: v for k, v in ENV.items() if k != key})


class InstanceFile(unittest.TestCase):
    def test_strict_file_and_test_only_keys(self):
        text = ''.join(f'{k}={v}\n' for k, v in ENV.items())
        self.assertEqual(common.parse_instance(text, complete=True)['PSY_GROUP_ID'], '-202')
        for line in ('PSY_OWNER_ID=0101', 'PSY_BOT_USERNAME=demo', 'PSY_PUBLIC_URL=https://fixture.invalid:0',
                     'PSY_COMMON_PATH=/tmp/psy_common.py'):
            with self.subTest(line=line), self.assertRaises(common.InstanceError) as caught:
                common.parse_instance(line + '\n')
            self.assertNotIn(line.partition('=')[2], str(caught.exception))
        with self.assertRaisesRegex(common.InstanceError, 'must differ'):
            common.parse_instance(text.replace('PSY_BOT_ID=303', 'PSY_BOT_ID=101'), complete=True)
        self.assertEqual(common.parse_instance(text.replace('PSY_BOT_ID=303', 'PSY_BOT_ID=101'))['PSY_BOT_ID'], '101')


class AtomicFiles(unittest.TestCase):
    def test_write_replaces_a_link_instead_of_following_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            victim = root / 'victim'
            victim.write_bytes(b'keep')
            state = root / 'state.json'
            state.symlink_to(victim)
            (root / 'state.tmp').symlink_to(victim)        # the fixed name the old writers used
            common.atomic_write(state, b'{"new": 1}')
            self.assertFalse(state.is_symlink())
            self.assertEqual(state.read_bytes(), b'{"new": 1}')
            self.assertEqual(victim.read_bytes(), b'keep')
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['state.json', 'state.tmp', 'victim'])

    def test_failed_write_keeps_the_old_file_and_leaves_no_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'record.json'
            target.write_bytes(b'old')
            with self.assertRaises(RuntimeError):
                with common.atomic_output(target, durable=False) as output:
                    output.write(b'partial')
                    raise RuntimeError('interrupted')
            self.assertEqual(target.read_bytes(), b'old')
            self.assertEqual([p.name for p in Path(folder).iterdir()], ['record.json'])
            common.atomic_write(target, b'shared', 0o644, durable=False)
            self.assertEqual((target.read_bytes(), target.stat().st_mode & 0o777), (b'shared', 0o644))

    def test_read_regular_refuses_links_and_fifos_and_reads_tails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'log').write_bytes(b'head' + b'x' * 100 + b'tail')
            self.assertEqual(common.read_regular(root / 'log', 4), b'head')
            self.assertEqual(common.read_regular(root / 'log', 4, tail=True), b'tail')
            (root / 'link').symlink_to(root / 'log')
            os.mkfifo(root / 'fifo')
            for name in ('link', 'fifo'):
                with self.subTest(name=name), self.assertRaises(OSError):
                    common.read_regular(root / name)

    def test_profile_env_value(self):
        text = 'OTHER=1\n  TELEGRAM_BOT_TOKEN = "value"\nTELEGRAM_BOT_TOKEN=second\n'
        self.assertEqual(common.env_value(text, 'TELEGRAM_BOT_TOKEN'), 'value')
        self.assertEqual(common.env_value('TELEGRAM_BOT_TOKEN=\nTELEGRAM_BOT_TOKEN=x\n', 'TELEGRAM_BOT_TOKEN'), '')
        self.assertIsNone(common.env_value('export TELEGRAM_BOT_TOKEN=x\n', 'TELEGRAM_BOT_TOKEN'))


class ReadOnlySqlite(unittest.TestCase):
    def make(self, folder):
        path = Path(folder) / 'queue.sqlite3'
        db = sqlite3.connect(path)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE jobs(state TEXT)')
        db.execute("INSERT INTO jobs VALUES('queued')")
        db.commit()
        return path, db

    def test_idle_wal_database_is_read_without_creating_sidecars(self):
        with tempfile.TemporaryDirectory() as folder:
            path, db = self.make(folder)
            db.close()   # the last connection checkpoints and removes -wal and -shm
            for side in ('-wal', '-shm'):
                Path(str(path) + side).unlink(missing_ok=True)
            os.chmod(folder, 0o500)
            try:
                reader = common.open_readonly(path)
                self.assertEqual(reader.execute('SELECT state FROM jobs').fetchall(), [('queued',)])
                reader.close()
                self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ['queue.sqlite3'])
            finally:
                os.chmod(folder, 0o700)

    def test_busy_wal_database_sees_uncheckpointed_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path, db = self.make(folder)
            db.execute("INSERT INTO jobs VALUES('processing')")
            db.commit()
            reader = common.open_readonly(path)
            try:
                self.assertEqual(reader.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)
                with self.assertRaises(sqlite3.OperationalError):
                    reader.execute("INSERT INTO jobs VALUES('x')")
            finally:
                reader.close()
                db.close()

    def test_links_and_fifos_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path, db = self.make(folder)
            db.close()
            (Path(folder) / 'link.sqlite3').symlink_to(path)
            os.mkfifo(Path(folder) / 'fifo.sqlite3')
            for name in ('link.sqlite3', 'fifo.sqlite3'):
                with self.subTest(name=name), self.assertRaises(OSError):
                    common.open_readonly(Path(folder) / name)


class BotApi(unittest.TestCase):
    token = '123456:' + 'T' * 35

    def answer(self, body):
        return lambda *args, **kwargs: io.BytesIO(json.dumps(body).encode())

    def test_token_shape_and_owner(self):
        self.assertEqual(common.bot_token(self.token), self.token)
        self.assertEqual(common.bot_token('303:' + 'x' * 30, 303), '303:' + 'x' * 30)
        for raw, bot in ((self.token, 303), ('12345:' + 'T' * 35, None), ('0123456:' + 'T' * 35, None),
                         ('123456:' + 'T' * 29, None), (self.token + '\n', None), ('303:' + 'x' * 30 + '!', 303)):
            with self.subTest(raw=raw), self.assertRaises(ValueError) as caught:
                common.bot_token(raw, bot)
            self.assertNotIn(raw.strip(), str(caught.exception))

    def test_request_encodings_and_result(self):
        with patch.object(common.urllib.request, 'urlopen', side_effect=self.answer({'ok': True, 'result': 7})) as opened:
            self.assertEqual(common.bot_call(self.token, 'getChatMember', {'chat_id': -202}, timeout=6), 7)
        request = opened.call_args.args[0]
        self.assertEqual((request.full_url, request.data, request.get_header('Content-type')),
                         (common.BOT_API + self.token + '/getChatMember', b'{"chat_id": -202}', 'application/json'))
        self.assertEqual(opened.call_args.kwargs, {'timeout': 6})
        with patch.object(common.urllib.request, 'urlopen', side_effect=self.answer({'ok': True, 'result': {}})) as opened:
            common.bot_call(self.token, 'sendMessage', {'chat_id': '101', 'text': 'a b'}, timeout=15, form=True)
            self.assertEqual(opened.call_args.args[0].data, b'chat_id=101&text=a+b')
            common.bot_call(self.token, 'getWebhookInfo', timeout=18)
            self.assertEqual(opened.call_args.args[0].data, b'')

    def test_failures_carry_status_and_retry_but_never_the_token(self):
        url = common.BOT_API + self.token + '/sendMessage'
        failures = (
            (urllib.error.HTTPError(url, 429, 'Too Many ' + self.token, {},
                                    io.BytesIO(b'{"ok":false,"parameters":{"retry_after":7}}')), ('http=429', 429, 7)),
            (urllib.error.HTTPError(url, 401, 'Unauthorized ' + self.token, {}, io.BytesIO(b'{}')), ('http=401', 401, None)),
            (urllib.error.URLError('no route to ' + url), ('network=URLError', None, None)),
        )
        for error, (reason, status, retry) in failures:
            with self.subTest(reason=reason), patch.object(common.urllib.request, 'urlopen', side_effect=error), \
                    self.assertRaises(common.BotApiError) as caught:
                common.bot_call(self.token, 'sendMessage', {'text': 'x'}, timeout=15)
            failure = caught.exception
            self.assertEqual((failure.reason, failure.status, failure.retry_after), (reason, status, retry))
            self.assertEqual(failure.kind, reason.partition('=')[0])
            self.assertTrue(failure.__suppress_context__)
            self.assertNotIn(self.token, repr(failure.args) + str(failure))
        declined = {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 3}}
        with patch.object(common.urllib.request, 'urlopen', side_effect=self.answer(declined)), \
                self.assertRaises(common.BotApiError) as caught:
            common.bot_call(self.token, 'sendMessage', {}, timeout=15)
        self.assertEqual((caught.exception.kind, caught.exception.reason, caught.exception.error_code,
                          caught.exception.retry_after), ('api', 'api=429', 429, 3))


class Loader(unittest.TestCase):
    """Every consumer carries the same few lines that load the module by explicit path."""

    @staticmethod
    def loader(relative):
        found = [node for node in ast.parse((REPO / relative).read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name == '_common']
        return found[0] if found else None

    def test_consumers_share_one_loader_and_the_installed_path(self):
        reference = self.loader(INSTALLED_CONSUMERS[0])
        self.assertEqual(reference.args.defaults[0].value, str(common.INSTALLED))
        repository_copy = r"_common\(Path\(__file__\)\.resolve\(\)\.parents\[1\] / ['\"]common/psy_common\.py['\"]\)"
        for relative in INSTALLED_CONSUMERS + RELEASE_TOOLS:
            text = (REPO / relative).read_text()
            with self.subTest(relative=relative):
                self.assertEqual(ast.dump(self.loader(relative)), ast.dump(reference))
                self.assertNotRegex(text, r'sys\.path\.(insert|append)\([^)]*common')
                if relative in RELEASE_TOOLS:
                    self.assertRegex(text, repository_copy)
                else:
                    self.assertIn('_common()', text)
        for script in ('ops/install-runtime', 'ops/install-observer'):
            self.assertIn('common/psy_common.py" /usr/local/lib/psy-common/psy_common.py', (REPO / script).read_text())

    def test_loader_reuses_a_loaded_module_and_leaves_sys_path_alone(self):
        namespace = {}
        source = ast.get_source_segment((REPO / INSTALLED_CONSUMERS[0]).read_text(),
                                        next(n for n in ast.parse((REPO / INSTALLED_CONSUMERS[0]).read_text()).body
                                             if isinstance(n, ast.FunctionDef) and n.name == '_common'))
        exec('import importlib.util, os, sys\n' + source, namespace)
        path = list(sys.path)
        with patch.dict(sys.modules):
            sys.modules.pop('psy_common', None)
            with patch.dict(os.environ, {'PSY_COMMON_PATH': str(REPO / 'common/psy_common.py')}):
                loaded = namespace['_common']()
            self.assertIs(sys.modules['psy_common'], loaded)
            self.assertEqual(Path(loaded.__file__), REPO / 'common/psy_common.py')
            with patch.dict(os.environ, {'PSY_COMMON_PATH': '/nonexistent/psy_common.py'}):
                self.assertIs(namespace['_common'](), loaded)
            sys.modules.pop('psy_common')
            with patch.dict(os.environ, {'PSY_COMMON_PATH': '/nonexistent/psy_common.py'}), self.assertRaises(OSError):
                namespace['_common']()
            self.assertNotIn('psy_common', sys.modules)
        self.assertEqual(sys.path, path)


if __name__ == '__main__':
    unittest.main()
