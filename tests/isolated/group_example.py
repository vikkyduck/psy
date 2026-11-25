#!/usr/bin/env python3
"""Current PSY group queue + Codex, with a temporary store and loopback Telegram receiver.

Run with the installed Hermes interpreter. No real Telegram credential is used.
The only permitted external HTTP destinations are the existing Codex/OpenAI hosts.
"""
import argparse
import asyncio
from contextlib import ExitStack
from email.parser import BytesParser
from email.policy import default as email_policy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import inspect
import json
import logging
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
import urllib.parse
import urllib.request


GROUP = -1009000000001
BOT = 900000002
OWNER = 900000001
MEMBER = 900000003
OTHER_MEMBER = 900000004
UPDATE = 880000001
INCOMING = 8801
FIXTURE_TOKEN = str(BOT) + ':ISOLATED_NOT_A_VALID_TELEGRAM_TOKEN'
READONLY_GIT_QUERIES = []
VERSION_STATUS_METADATA_STUB = []
SYNTHETIC_SOUL = 'Synthetic acceptance profile. No personal instructions or conversation data.\n'


class IsolationViolation(RuntimeError):
    pass


def initialize_transport_without_startup(dependencies):
    """Load the real transport with only entry-point maintenance suppressed."""
    if 'hermes_bootstrap' in sys.modules or 'agent.process_bootstrap' in sys.modules:
        raise IsolationViolation('bootstrap loaded before isolated initialization')
    recovery = importlib.import_module('hermes_cli._early_recovery')
    launch = importlib.import_module('hermes_cli.venv_sync')
    environments = importlib.import_module('pm.environments')
    with ExitStack() as stack:
        suppressed = {}
        for module, name, result in ((recovery, 'restore_interrupted_pull', False),
                                     (launch, 'prepare_launch', None),
                                     (recovery, 'recover_if_needed', False),
                                     (environments, 'activate_dependencies', None)):
            suppressed[name] = stack.enter_context(patch.object(module, name, return_value=result))
        stack.enter_context(patch.dict(os.environ, {'HERMES_DISABLE_LAZY_INSTALLS': '1'}))
        transport = importlib.import_module('agent.process_bootstrap')
        if not callable(getattr(transport, 'enable_happy_eyeballs_on_client', None)):
            raise IsolationViolation('real transport helper unavailable')
        if str(dependencies) not in sys.path:
            raise IsolationViolation('selected dependency path was displaced')
        counts = {name: function.call_count for name, function in suppressed.items()}
    return {'mode': 'real transport with entry-point maintenance suppressed', 'suppressed_calls': counts}


def fixture_runtime_capture(route, fixture, model, patches):
    """Keep real auth/config reads, but snapshot only an explicit synthetic profile."""
    fixture = fixture.resolve()
    profile = fixture / 'synthetic-profile'
    profile.mkdir(mode=0o700)
    configured = {'model': {'provider': model['provider'], 'default': model['default']}}
    for name, value in (('config.yaml', json.dumps(configured, ensure_ascii=False) + '\n'),
                        ('SOUL.md', SYNTHETIC_SOUL)):
        descriptor = os.open(profile / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(value)
    capture = route.capture_runtime
    def synthetic_capture(selected_model, **options):
        root = Path(options.get('root') or fixture / 'runtime-snapshots').resolve()
        if not root.is_relative_to(fixture):
            raise IsolationViolation('runtime snapshot outside isolated fixture')
        if 'profile' in options and Path(options['profile']).resolve() != profile:
            raise IsolationViolation('runtime snapshot requested a non-synthetic profile')
        return capture(selected_model, **{**options, 'profile': profile, 'root': root})
    patches.enter_context(patch.object(route, 'capture_runtime', synthetic_capture))
    return profile


def durable_acceptance(store, fixture, profile, *, expected_effort=None):
    """Read only the fixture after queue cleanup, then expose content-free evidence."""
    key = 'update:' + str(UPDATE)
    report = store.explain_request(key, uid=MEMBER, topic=0)
    assert report is not None and report['state'] == 'finished', 'durable request explanation missing'
    assert store.explain_request(key, uid=OTHER_MEMBER, topic=0) is None
    assert store.explain_request(key, uid=MEMBER, topic=1) is None
    saved = store.result_details(MEMBER, 0, report['turn_id'])
    assert saved is not None and saved['delivery_status'] == 'confirmed', 'durable delivery receipt missing'
    events = {item['event']: item['details'] for item in report['events']}
    assert 'result_saved' in events and 'delivery_confirmed' in events and 'queue_completed' in events
    receipt = events['queue_completed']
    assert receipt['delivery_confirmed'] and receipt['confirmed_parts'] > 0 and receipt['pending_parts'] == 0
    with store.database() as database:
        assert database.execute('SELECT count(*) FROM requests WHERE id=?', (UPDATE,)).fetchone()[0] == 0
        assert database.execute('SELECT count(*) FROM checkpoints WHERE request_id=?', (UPDATE,)).fetchone()[0] == 0
    generation = saved['quality_metadata'].get('generation_audit', {})
    if expected_effort is not None:
        assert saved['task_status'] == 'complete', 'task was not accepted as complete'
        assert saved['generation_complete'] is True, 'API completion was not retained'
        efforts = generation.get('effort_counts', {})
        assert set(efforts) == {expected_effort} and efforts[expected_effort] > 0, 'generation did not use maximum effort'
        assert generation.get('fallback_stages') == 0
        assert generation.get('effort_policy') == 'maximum_supported_no_downgrade'
        audited = events['result_saved']
        assert audited['task_status'] == 'complete' and audited['generation_complete'] is True
        assert audited['metadata']['generation_audit']['effort_counts'] == efforts
    # Verify every captured profile without reading the real profile for comparison.
    root = fixture / 'runtime-snapshots'
    snapshots = sorted(root.iterdir())
    assert snapshots, 'runtime snapshot missing'
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for snapshot in snapshots:
        assert re.fullmatch('[0-9a-f]{64}', snapshot.name)
        assert snapshot.is_dir() and not snapshot.is_symlink() and snapshot.resolve().is_relative_to(fixture)
        for path in (snapshot, *snapshot.rglob('*')):
            assert not path.is_symlink() and not path.stat().st_mode & 0o077, 'snapshot is not private'
        manifest = json.loads((snapshot / 'manifest.json').read_text())
        for name in ('config.yaml', 'SOUL.md'):
            body = (snapshot / 'profile' / name).read_bytes()
            assert body == (profile / name).read_bytes(), 'snapshot contains a non-synthetic profile'
            assert manifest['profile/' + name] == hashlib.sha256(body).hexdigest()
    runtime = saved['quality_metadata']['runtime']
    assert runtime['snapshot_verified'] is True and (root / runtime['snapshot_id']).is_dir()
    assert SYNTHETIC_SOUL.strip() not in json.dumps(report), 'profile text entered technical diagnostics'
    return {'durable_diagnostics_after_cleanup': True, 'durable_task_status': saved['task_status'],
            'durable_generation_complete': saved['generation_complete'],
            'durable_maximum_effort_verified': expected_effort is not None,
            'generation_effort': expected_effort, 'durable_delivery_status': saved['delivery_status'],
            'durable_confirmed_parts': receipt['confirmed_parts'],
            'runtime_snapshot_private': True, 'runtime_snapshot_count': len(snapshots),
            'synthetic_profile_only': True}


class Receiver:
    def __init__(self):
        self.calls = []
        self.messages = []
        self.sequence = 9900
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                if length > 4 * 1024**2:
                    self.send_error(413)
                    return
                data = self.rfile.read(length)
                kind = self.headers.get('Content-Type', '')
                if 'application/json' in kind:
                    params = json.loads(data)
                elif 'multipart/form-data' in kind:
                    message = BytesParser(policy=email_policy).parsebytes(
                        ('Content-Type: ' + kind + '\r\n\r\n').encode() + data)
                    params = {}
                    for part in message.iter_parts():
                        key = part.get_param('name', header='content-disposition')
                        value = part.get_payload(decode=True)
                        params[key] = value if part.get_filename() else value.decode()
                else:
                    params = {k: values[-1] for k, values in urllib.parse.parse_qs(data.decode()).items()}
                try:
                    result = receiver.answer(self.path, params)
                    body = json.dumps({'ok': True, 'result': result}).encode()
                    self.send_response(200)
                except IsolationViolation:
                    body = json.dumps({'ok': False, 'error_code': 403,
                                       'description': 'isolated receiver rejected destination'}).encode()
                    self.send_response(403)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def answer(self, path, params):
        if not path.startswith('/bot' + FIXTURE_TOKEN + '/'):
            raise IsolationViolation('unexpected fixture credential')
        method = path.rsplit('/', 1)[-1].casefold()
        self.calls.append(method)
        if method == 'getme':
            return {'id': BOT, 'first_name': 'Isolated PSY', 'username': 'PsyFixtureBot', 'is_bot': True}
        chat = int(params.get('chat_id', '0'))
        if chat != GROUP:
            raise IsolationViolation('destination is not the synthetic group')
        if method in ('sendchataction', 'setmessagereaction'):
            return True
        if method == 'getchatmember':
            uid = int(params.get('user_id', '0'))
            if uid not in (MEMBER, OTHER_MEMBER, BOT):
                raise IsolationViolation('unexpected synthetic member')
            return {'status': 'member', 'user': {'id': uid, 'first_name': 'Fixture', 'is_bot': uid == BOT}}
        if method not in ('sendmessage', 'senddocument'):
            raise IsolationViolation('unexpected fixture method')
        self.sequence += 1
        result = {'message_id': self.sequence, 'date': int(time.time()),
                  'chat': {'id': GROUP, 'type': 'supergroup', 'title': 'Isolated PSY fixture'},
                  'from': {'id': BOT, 'is_bot': True, 'first_name': 'Isolated PSY'}}
        if method == 'sendmessage':
            result['text'] = params.get('text', '')
        else:
            result['document'] = {'file_id': 'fixture-result', 'file_unique_id': 'fixture-result-unique',
                                  'file_name': 'Answer.txt', 'file_size': 1}
        self.messages.append({'method': method, 'chat_id': chat,
                              'message_id': self.sequence, 'text': params.get('text', '')})
        return result


class NetworkGuard:
    """Allow this one loopback receiver and existing OpenAI endpoints, fail closed otherwise."""
    HOSTS = {'chatgpt.com', 'auth.openai.com'}

    def __init__(self, port, allow_model):
        self.port = port
        self.allow_model = allow_model
        self.addresses = set()
        self.external_http = []
        self.codex_http = 0
        self.denied = []
        self.stack = ExitStack()
        self.real_getaddrinfo = socket.getaddrinfo
        self.real_connect = socket.socket.connect
        self.real_connect_ex = socket.socket.connect_ex

    def allowed_url(self, value):
        parsed = urllib.parse.urlsplit(str(value))
        if parsed.hostname == '127.0.0.1' and parsed.port == self.port:
            return
        if self.allow_model and parsed.scheme == 'https' and parsed.hostname in self.HOSTS:
            self.external_http.append(parsed.hostname)
            if parsed.hostname == 'chatgpt.com' and parsed.path.startswith('/backend-api/codex'):
                self.codex_http += 1
            return
        self.denied.append('http')
        raise IsolationViolation('non-allowlisted HTTP destination')

    def __enter__(self):
        import httpx
        def dns(host, *args, **kwargs):
            name = host.decode() if isinstance(host, bytes) else str(host)
            if name not in {'127.0.0.1', 'localhost', '::1'} and not (
                    self.allow_model and (name in self.HOSTS or name in self.addresses)):
                self.denied.append('dns')
                raise IsolationViolation('non-allowlisted DNS destination')
            records = self.real_getaddrinfo(host, *args, **kwargs)
            if name in self.HOSTS:
                self.addresses.update(row[4][0] for row in records)
            return records

        def connection_allowed(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                host, port = str(address[0]), address[1]
                local = host in {'127.0.0.1', '::1'} and port == self.port
                cloud = self.allow_model and port == 443 and (host in self.HOSTS or host in self.addresses)
                if not (local or cloud):
                    self.denied.append('socket')
                    raise IsolationViolation('non-allowlisted TCP destination')
        def connect(sock, address):
            connection_allowed(sock, address)
            return self.real_connect(sock, address)
        def connect_ex(sock, address):
            connection_allowed(sock, address)
            return self.real_connect_ex(sock, address)

        real_sync, real_async = httpx.Client.send, httpx.AsyncClient.send
        def sync_send(client, request, *args, **kwargs):
            self.allowed_url(request.url)
            return real_sync(client, request, *args, **kwargs)
        async def async_send(client, request, *args, **kwargs):
            self.allowed_url(request.url)
            return await real_async(client, request, *args, **kwargs)
        real_urlopen = urllib.request.urlopen
        def urlopen(request, *args, **kwargs):
            self.allowed_url(request.full_url if isinstance(request, urllib.request.Request) else request)
            return real_urlopen(request, *args, **kwargs)
        self.stack.enter_context(patch.object(socket, 'getaddrinfo', dns))
        self.stack.enter_context(patch.object(socket.socket, 'connect', connect))
        self.stack.enter_context(patch.object(socket.socket, 'connect_ex', connect_ex))
        self.stack.enter_context(patch.object(httpx.Client, 'send', sync_send))
        self.stack.enter_context(patch.object(httpx.AsyncClient, 'send', async_send))
        self.stack.enter_context(patch.object(urllib.request, 'urlopen', urlopen))
        return self

    def __exit__(self, *args):
        self.stack.close()


def install_storage_audit(fixture_root, actual_home, checkout, *, pdf_reader=None):
    """A missed store override must fail before accessing a production business database."""
    fixture_root = fixture_root.resolve()
    checkout = checkout.resolve()
    business = [(actual_home / name).resolve() for name in ('vault', 'media-vault')]
    observed = {'pdf_subprocesses': 0, 'denials': 0}
    pdf_permit = threading.local()
    pdf_command = None
    pdf_env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
    if pdf_reader is not None:
        pdf_reader = Path(pdf_reader)
        if pdf_reader.is_symlink() or not pdf_reader.is_file() or pdf_reader.name != 'psy_source_evidence.py':
            raise IsolationViolation('PDF reader must be the selected regular production module')
        pdf_reader = pdf_reader.resolve(strict=True)
        pdf_hash = hashlib.sha256(pdf_reader.read_bytes()).digest()
        pdf_command = ['/usr/bin/python3', '-I', str(pdf_reader), '--pdf']

    def deny(reason):
        observed['denials'] += 1
        raise IsolationViolation(reason)

    def pdf_run_allowed(argv, args, kwargs):
        # Opt-in only. This does not permit general Python or PDF utilities. The
        # selected production decoder retains its own seccomp/Landlock sandbox.
        if (pdf_command is None or not isinstance(argv, (list, tuple))
                or list(argv) != pdf_command or args
                or set(kwargs) != {'input', 'stdout', 'stderr', 'timeout', 'env', 'check'}
                or kwargs['env'] != pdf_env or kwargs['stderr'] != subprocess.DEVNULL
                or kwargs['timeout'] != 32 or kwargs['check'] is not True):
            return False
        data = kwargs['input']
        if not isinstance(data, bytes) or not data.startswith(b'%PDF-') or len(data) > 8 * 1024 * 1024:
            return False
        if pdf_reader.is_symlink() or hashlib.sha256(pdf_reader.read_bytes()).digest() != pdf_hash:
            return False
        try:
            descriptor = kwargs['stdout'].fileno()
            output = os.fstat(descriptor)
            # The production decoder runs on Linux. Resolve its already-open
            # output descriptor, including TemporaryFile's unlinked pathname.
            path = Path(os.readlink('/proc/self/fd/' + str(descriptor)).removesuffix(' (deleted)')).resolve()
            return (stat.S_ISREG(output.st_mode) and output.st_uid == os.geteuid()
                    and output.st_mode & 0o077 == 0 and path.is_relative_to(fixture_root))
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return False
    # Version metadata is imported by auth constants, even with the Codex provider.
    # Allow only its exact immutable queries, with hooks, locks, credentials,
    # prompts and every Git transport disabled. No inherited environment enters Git.
    git_prefix = ['/usr/bin/git', '--no-pager', '--no-optional-locks',
                  '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
                  '-c', 'core.untrackedCache=false', '-c', 'credential.helper=',
                  '-c', 'core.askPass=/bin/false', '-c', 'log.showSignature=false',
                  '-c', 'gpg.program=/bin/false', '-c', 'gpg.ssh.program=/bin/false']
    git_env = {'PATH': '/usr/bin:/bin', 'HOME': str(fixture_root), 'LC_ALL': 'C.UTF-8',
               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': '/dev/null',
               'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_OPTIONAL_LOCKS': '0',
               'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '/bin/false',
               'SSH_ASKPASS': '/bin/false', 'GIT_SSH_COMMAND': '/bin/false',
               'GIT_ALLOW_PROTOCOL': '', 'GIT_NO_LAZY_FETCH': '1'}
    def query_allowed(parts):
        exact = {('rev-parse', 'HEAD'), ('rev-parse', '--short=7', 'HEAD'),
                 ('branch', '--show-current'), ('log', '-1', '--format=%ct', 'HEAD'),
                 ('tag', '--merged', 'HEAD', '--list', 'v[0-9]*'),
                 ('describe', '--tags', '--long', '--match', 'v2[0-9][0-9][0-9].*', 'HEAD')}
        return (tuple(parts) in exact
                or len(parts) == 3 and parts[:2] == ['rev-list', '--count']
                   and re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+\.\.HEAD', parts[2]) is not None
                or len(parts) == 2 and parts[0] == 'show'
                   and re.fullmatch(r'v2[0-9]{3}\.[0-9]+\.[0-9]+:pyproject\.toml', parts[1]) is not None)
    original_run = subprocess.run
    def metadata_run(argv, *args, **kwargs):
        if pdf_run_allowed(argv, args, kwargs):
            # The audit hook consumes this one-use permit on this same thread.
            # Direct Popen calls, even with identical arguments, stay forbidden.
            pdf_permit.active = True
            try:
                return original_run(argv, **kwargs)
            finally:
                pdf_permit.active = False
        if (not isinstance(argv, (list, tuple)) or not argv
                or argv[0] not in ('git', '/usr/bin/git', '/bin/git') or args
                or set(kwargs) - {'capture_output', 'text', 'encoding', 'errors', 'timeout', 'cwd', 'check'}
                or Path(kwargs.get('cwd') or '.').resolve() != checkout):
            deny('subprocess is not an approved Git metadata probe or opted-in PDF decoder')
        parts = list(argv[1:])
        if parts[:1] == ['--no-optional-locks']:
            parts = parts[1:]
        if tuple(parts) in {('status', '--porcelain', '-uno'), ('status', '--porcelain')}:
            # A real status may invoke worktree clean/process filters below the
            # Python process guard. Substitute only this non-behavioral dirty flag.
            # The provider, model selection and model response remain real.
            VERSION_STATUS_METADATA_STUB.append(True)
            return subprocess.CompletedProcess(list(argv), 0, ' M isolated-fixture\n', '')
        if not query_allowed(parts):
            deny('Git arguments are outside the immutable version allowlist')
        timeout = kwargs.get('timeout', 3)
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 5:
            deny('invalid Git metadata probe timeout')
        return original_run([*git_prefix, *parts], env=dict(git_env), **kwargs)
    subprocess.run = metadata_run
    def audit(event, args):
        if event == 'subprocess.Popen':
            executable, argv, cwd, env = args
            if (pdf_command is not None and getattr(pdf_permit, 'active', False)
                    and executable == pdf_command[0] and argv == pdf_command
                    and cwd is None and env == pdf_env):
                pdf_permit.active = False
                observed['pdf_subprocesses'] += 1
                return
            if (executable != '/usr/bin/git' or not isinstance(argv, list)
                    or argv[:len(git_prefix)] != git_prefix
                    or not query_allowed(argv[len(git_prefix):])
                    or Path(cwd or '.').resolve() != checkout or env != git_env):
                deny('subprocesses are disabled except immutable Git metadata and opted-in PDF decoding')
            READONLY_GIT_QUERIES.append(argv[len(git_prefix)])
        if event == 'sqlite3.connect':
            path = str(args[0])
            if path == ':memory:':
                return
            if path.startswith('file:'):
                path = urllib.parse.urlsplit(path).path
            resolved = Path(path).resolve()
            if not resolved.is_relative_to(fixture_root):
                deny('database outside isolated fixture')
        if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if any(path.is_relative_to(root) for root in business):
                deny('production business files are excluded')
    sys.addaudithook(audit)
    return observed


async def example(args, fixture, receiver, guard):
    from telegram import Bot, Update
    from plugins.platforms.telegram import psy_group_route as route, psy_group_queue as queue
    from plugins.platforms.telegram import psy_group_store as store, psy_instance as instance
    import hermes_cli.config as config

    originals = {'database': store.database, 'lease': store.lease}
    model = {'provider': 'openai-codex', 'default': 'isolated-fixture-model'} if args.stub_model else config.load_config_readonly()['model']
    if model.get('provider') != 'openai-codex':
        raise IsolationViolation('the configured provider is not openai-codex')
    with ExitStack() as patches:
        profile = fixture_runtime_capture(route, fixture, model, patches)
        for module in (instance, route, queue):
            for name, value in [('BOT', BOT), ('GROUP', GROUP), ('OWNER', OWNER)]:
                if hasattr(module, name):
                    patches.enter_context(patch.object(module, name, value))
        patches.enter_context(patch.object(store, 'ROOT', fixture))
        patches.enter_context(patch.object(store, 'database', lambda *_args, **_kw: originals['database'](fixture)))
        patches.enter_context(patch.object(store, 'lease', lambda **_kw: originals['lease'](root=fixture)))
        patches.enter_context(patch.object(config, 'load_config', lambda: {'model': dict(model)}))
        model_calls = []
        model_failures = []
        original_respond = route.respond
        def respond(*arguments, **options):
            model_calls.append(True)
            if args.stub_model:
                return SimpleNamespace(text='42', images=[])
            try:
                return original_respond(*arguments, **options)
            except Exception as error:
                model_failures.append(error)
                raise
        patches.enter_context(patch.object(route, 'respond', respond))
        bot = Bot(FIXTURE_TOKEN, base_url=f'http://127.0.0.1:{receiver.port}/bot',
                  base_file_url=f'http://127.0.0.1:{receiver.port}/file/bot')
        await bot.initialize()
        class Application:
            running = True
            adapter = SimpleNamespace()
            def __init__(self):
                self.bot = bot
            def create_task(self, coroutine, **options):
                return asyncio.create_task(coroutine, **options)
        app = Application()
        async def finish_group_task(timeout):
            task = app.adapter._psy_group_task
            expires = time.monotonic() + timeout
            while not task.done():
                if model_failures:
                    # Do not wait through production retry schedules in a one-shot acceptance.
                    # The model call has already ended, so stopping this fixture cannot cancel work.
                    raise model_failures[0]
                if time.monotonic() >= expires:
                    raise TimeoutError('isolated group acceptance timed out')
                await asyncio.sleep(0.05)
            return await task
        update = Update.de_json({'update_id': UPDATE, 'message': {
            'message_id': INCOMING, 'date': int(time.time()),
            'chat': {'id': GROUP, 'type': 'supergroup', 'title': 'Isolated PSY fixture'},
            'from': {'id': MEMBER, 'first_name': 'Synthetic participant', 'is_bot': False},
            'text': 'Сколько будет 17 плюс 25? Ответь только числом.'}}, bot)
        try:
            assert route.scope(update) == 'group'
            assert MEMBER != OWNER
            assert await route.intercept(app, update) is True
            await finish_group_task(args.timeout)
            messages = list(receiver.messages)
            assert len(messages) == 1, 'expected one answer only'
            assert messages[0]['chat_id'] == GROUP
            normalized = re_normalize(messages[0]['text'])
            assert normalized == '42', 'model response was not the requested number'
            history = store.history(MEMBER, 0)
            assert len(history) == 2 and re_normalize(history[-1]['content']) == '42'
            assert store.history(OTHER_MEMBER, 0) == []
            with store.database() as db:
                complete = db.execute('SELECT count(*) FROM completed_updates WHERE id=?', (UPDATE,)).fetchone()[0]
                pending = db.execute('SELECT count(*) FROM requests').fetchone()[0]
                bound = db.execute('SELECT count(*) FROM message_sources WHERE uid=? AND message_id=?',
                                   (MEMBER, messages[0]['message_id'])).fetchone()[0]
                db_path = Path(db.execute('PRAGMA database_list').fetchone()[2]).resolve()
            assert complete == 1 and pending == 0 and bound == 1
            assert db_path.is_relative_to(fixture)
            expected_effort = None
            if not args.stub_model:
                from plugins.platforms.telegram.psy_group_client import _maximum_effort
                expected_effort = _maximum_effort(model['default'])
            durable = durable_acceptance(store, fixture, profile, expected_effort=expected_effort)
            await route.intercept(app, update)
            await finish_group_task(10)
            assert len(receiver.messages) == 1, 'duplicate update produced a second answer'
            assert len(model_calls) == 1, 'duplicate update repeated model generation'
            if not args.stub_model:
                assert guard.codex_http > 0, 'no actual Codex HTTP request observed'
            assert not guard.denied, 'an unexpected network attempt occurred'
            sources = {}
            for name, module in list(sys.modules.items()):
                path = getattr(module, '__file__', None)
                if name.startswith('plugins.platforms.telegram.psy_group') and path and Path(path).is_file():
                    sources[name.rsplit('.', 1)[-1]] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            return {'status': 'passed', 'real_codex': not args.stub_model,
                    'provider': model['provider'], 'model': model['default'],
                    'synthetic_non_owner_member': True, 'synthetic_group_destination': True,
                    'answer': messages[0]['text'], 'captured_group_answers': len(receiver.messages),
                    'duplicate_update_extra_answers': 0, 'other_member_history_empty': True,
                    'history_and_outgoing_message_bound': True, 'completed_jobs': complete,
                    'pending_jobs': pending, 'temporary_database_verified': True,
                    'telegram_transport': 'loopback HTTP Bot API receiver with fake credential',
                    'real_telegram_requests': 0, 'guard_denials': len(guard.denied),
                    'openai_http_requests': len(guard.external_http),
                    'codex_http_requests': guard.codex_http, 'group_model_invocations': len(model_calls),
                    'readonly_git_metadata_queries': len(READONLY_GIT_QUERIES),
                    'version_status_metadata_stub': bool(VERSION_STATUS_METADATA_STUB),
                    'external_hosts': sorted(set(guard.external_http)), 'source_sha256': sources, **durable}
        finally:
            app.running = False
            for name in ('_psy_group_task', '_psy_group_ticker', '_psy_owner_watcher'):
                task = getattr(app.adapter, name, None)
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            await bot.shutdown()


def re_normalize(text):
    return text.strip().rstrip('.!').strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, default=Path('/home/hermes/.hermes/hermes-agent'))
    parser.add_argument('--stub-model', action='store_true', help='explicit wiring-only mode, not real model acceptance')
    parser.add_argument('--timeout', type=float, default=600)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    actual_home = Path(os.environ.get('HERMES_HOME', '/home/hermes/.hermes')).resolve()
    # Leave the shared Codex authentication home in place, never clone refresh credentials.
    for key in list(os.environ):
        if key in ('TELEGRAM_BOT_TOKEN', 'BOT_TOKEN', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY', 'NOUS_API_KEY'):
            os.environ.pop(key, None)
    os.environ.update(PSY_OWNER_ID=str(OWNER), PSY_GROUP_ID=str(GROUP), PSY_BOT_ID=str(BOT),
                      PSY_BOT_USERNAME='PsyFixtureBot', PSY_PUBLIC_URL='https://example.invalid',
                      TELEGRAM_BOT_TOKEN=FIXTURE_TOKEN, BOT_TOKEN=FIXTURE_TOKEN)
    sys.path.insert(0, str(args.checkout.resolve()))
    with tempfile.TemporaryDirectory(prefix='psy-isolated-group-') as temp:
        fixture = Path(temp).resolve()
        install_storage_audit(fixture, actual_home, args.checkout)
        # Read the already committed dependency selection. Full hermes_bootstrap
        # also runs source-update recovery, so it must not be used by this fixture.
        # sys.path alone avoids executing .pth hooks or acquiring runtime leases.
        from pm.environments import committed_venv, site_packages
        environment = committed_venv(args.checkout.resolve())
        if environment is None:
            raise IsolationViolation('no committed dependency environment')
        dependencies = site_packages(environment)
        if not dependencies.is_dir():
            raise IsolationViolation('committed dependency libraries are missing')
        sys.path.insert(1, str(dependencies))
        with Receiver() as receiver:
            with NetworkGuard(receiver.port, allow_model=not args.stub_model) as guard:
                bootstrap = initialize_transport_without_startup(dependencies)
                result = asyncio.run(example(args, fixture, receiver, guard))
                result['bootstrap'] = bootstrap
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        safe = {'status': 'failed', 'error_class': type(error).__name__}
        if isinstance(error, IsolationViolation):
            safe['isolation_reason'] = str(error)
        cursor = error.__traceback__
        frames = []
        while cursor:
            if cursor.tb_frame.f_code.co_name != 'audit':
                frames.append({'file': Path(cursor.tb_frame.f_code.co_filename).name,
                               'function': cursor.tb_frame.f_code.co_name,
                               'line': cursor.tb_lineno})
            cursor = cursor.tb_next
        if frames:
            safe['error_frames'] = frames[-8:]
        cursor = error.__traceback__
        while cursor and cursor.tb_next:
            cursor = cursor.tb_next
        if cursor:
            safe['error_source'] = {'file': Path(cursor.tb_frame.f_code.co_filename).name,
                                    'function': cursor.tb_frame.f_code.co_name,
                                    'line': cursor.tb_lineno}
        for name in ('kind', 'stage'):
            value = getattr(error, name, None)
            if isinstance(value, str) and value.replace('_', '').isalnum():
                safe[name] = value
        print(json.dumps(safe, ensure_ascii=False))
        raise SystemExit(1)
