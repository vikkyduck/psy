#!/usr/bin/env python3
"""Current Mini App DOM handler -> real HTTP handlers -> real offline Whisper -> loopback group.

Only the Telegram peer and the privileged broker transport are replaced. The real current auth/grant
dispatcher, upload handlers, JS, worker, receipt code, ASR socket service and standalone decoder
are used. All state and all credentials are synthetic and temporary. Run this in a private
network namespace as a non-root user for a second, kernel-enforced network boundary.
"""
import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import pwd
import shutil
import signal
import socket
import sys
import tempfile
import time
from urllib.parse import urlencode, unquote
from unittest.mock import patch


OWNER, GROUP, BOT, MEMBER = 900000001, -1009000000001, 900000002, 900000003
TOKEN = str(BOT) + ':' + 'fixture_only_not_a_real_credential_123456789'


def init_data(uid):
    fields = {'auth_date': str(int(time.time())), 'start_param': 'psy_group',
              'user': json.dumps({'id': uid, 'first_name': 'Synthetic participant', 'is_bot': False})}
    key = hmac.digest(b'WebAppData', TOKEN.encode(), 'sha256')
    fields['hash'] = hmac.new(key, '\n'.join(f'{k}={v}' for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def guarded_socket_connect(original, root):
    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            try:
                allowed = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                allowed = False
            if not allowed:
                raise AssertionError('External network is forbidden in the isolated upload example')
        elif sock.family == socket.AF_UNIX:
            name = os.fsdecode(address)
            if not name or not Path(name).is_relative_to(root):
                raise AssertionError('A production Unix service is forbidden in the isolated upload example')
        return original(sock, address)
    return connect


def fixture_descendants(pid):
    """Only descendants of this fixture's ASR daemon, never arbitrary same-UID processes."""
    parents = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit(): continue
        try:
            ppid = int((entry / 'stat').read_bytes().rsplit(b')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError): continue
        parents.setdefault(ppid, []).append(int(entry.name))
    result, pending = [], [pid]
    while pending:
        for child in parents.get(pending.pop(), []):
            result.append(child); pending.append(child)
    return result


async def main(args):
    if os.geteuid() != pwd.getpwnam('psy-deliver').pw_uid:
        raise RuntimeError('Run the fixture as the existing non-root delivery user')
    interfaces = {line.split(':', 1)[0].strip() for line in Path('/proc/net/dev').read_text().splitlines()[2:] if ':' in line}
    if interfaces != {'lo'}:
        raise RuntimeError('The fixture requires a private network namespace with loopback only')
    source = args.source.resolve(strict=True)
    media = args.media.resolve(strict=True)
    if source.stat().st_size <= 20 * 1024 ** 2:
        raise ValueError('The synthetic input must be larger than 20 MiB')
    with tempfile.TemporaryDirectory(prefix='psy-isolated-upload-') as temp, ExitStack() as scope:
        root = Path(temp).resolve()
        credentials = root / 'fixture-credentials'
        credentials.mkdir(mode=0o700)
        (credentials / 'telegram').write_text(TOKEN)
        (credentials / 'recording-signing').write_bytes(os.urandom(32))
        for p in credentials.iterdir(): p.chmod(0o600)
        # Remove inherited service configuration before importing any runtime module.
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(('PSY_', 'HERMES_', 'TELEGRAM_', 'OPENAI_', 'NOUS_', 'OPENROUTER_'))
                 and k != 'CREDENTIALS_DIRECTORY'}
        clean.update(PSY_OWNER_ID=str(OWNER), PSY_GROUP_ID=str(GROUP), PSY_BOT_ID=str(BOT),
            PSY_BOT_USERNAME='SyntheticFixtureBot', PSY_PUBLIC_URL='https://fixture.invalid',
            PSY_UPLOAD_ROOT=str(root / 'state'), PSY_UPLOAD_FILES=str(root / 'audio'),
            PSY_RECEIPT_ROOT=str(root / 'receipts'), PSY_ASR_PROGRESS=str(root / 'progress'),
            HERMES_HOME=str(root / 'home'), CREDENTIALS_DIRECTORY=str(credentials),
            PSY_COMMON_PATH=str(args.common.resolve(strict=True)), PSY_ACCESS_SOCKET=str(root / 'member.sock'))
        scope.enter_context(patch.dict(os.environ, clean, clear=True))
        sys.path.insert(0, str(media))
        import psy_upload_common as C
        import recording_grant as grants
        import recording_receipts as receipts
        import server
        import worker
        import asr_client
        import access_broker
        from aiohttp import ClientSession, ClientTimeout, web
        from aiohttp.test_utils import TestServer, TestClient
        for path in (C.FILES, receipts.root(), root / 'progress'):
            path.mkdir(mode=0o700, parents=True)
        assert C.GROUP == GROUP and C.OWNER == OWNER and C.BOT_ID == BOT
        assert C.ROOT.is_relative_to(root) and C.FILES.is_relative_to(root) and receipts.root().is_relative_to(root)
        C.initialize()
        receipts.initialize()
        # Reject an accidental live Telegram call even outside the private network namespace.
        scope.enter_context(patch.object(socket.socket, 'connect', guarded_socket_connect(socket.socket.connect, root)))
        async def handle_broker(reader, writer):
            try:
                payload = json.loads(await reader.readline())
                result = access_broker.dispatch(payload, TOKEN, lambda uid, _token: uid == MEMBER)
                writer.write(json.dumps(result).encode() + b'\n')
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        broker = await asyncio.start_unix_server(handle_broker, path=str(root / 'member.sock'))
        deliveries = []
        membership_reads = []
        async def get_member(request):
            data = await request.post()
            assert int(data['chat_id']) == GROUP
            membership_reads.append(int(data['user_id']))
            return web.json_response({'ok': True, 'result': {'status': 'member' if int(data['user_id']) == MEMBER else 'left'}})
        async def send_document(request):
            data = await request.post()
            assert int(data['chat_id']) == GROUP
            # aiohttp's outgoing multipart encoder percent-encodes a Unicode filename.
            # Telegram accepts the multipart form, while this test receiver exposes the raw name.
            assert unquote(data['document'].filename) == 'Расшифровка.txt'
            assert data['document'].content_type.startswith('text/plain')
            text = data['document'].file.read().decode('utf-8')
            assert text.strip()
            deliveries.append({'chat': int(data['chat_id']), 'text': text})
            return web.json_response({'ok': True, 'result': {'message_id': 9001}})
        fake = web.Application(client_max_size=20 * 1024 ** 2)
        fake.add_routes([web.post('/bot' + TOKEN + '/getChatMember', get_member),
                         web.post('/bot' + TOKEN + '/sendDocument', send_document)])
        asr_socket = root / 'asr.sock'
        asr_private = root / 'asr-private'
        asr_env = dict(PATH='/usr/bin:/bin', HOME=str(root), TMPDIR=str(root),
            HERMES_HOME=str(root / 'home'), PSY_ASR_STANDALONE='1', HF_HUB_OFFLINE='1',
            PSY_WHISPER_MODEL=str(args.model.resolve(strict=True)), PSY_ASR_INPUT=str(C.FILES),
            PSY_ASR_OUTPUT=str(asr_private), PSY_ASR_PROGRESS=str(root / 'progress'),
            OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
        bootstrap = ('import sys\nfrom pathlib import Path\n'
            + 'sys.path.insert(0,' + repr(str(media)) + ')\n'
            + 'import asr_service\nasr_service.SOCKET=Path(' + repr(str(asr_socket)) + ')\n'
            + 'asr_service.main()\n')
        asr_log = root / 'fixture-asr.stderr'
        with asr_log.open('wb') as error_stream:
            asr_process = await asyncio.create_subprocess_exec(str(args.asr_python), '-I', '-c', bootstrap,
                env=asr_env, cwd=root, stdout=asyncio.subprocess.DEVNULL, stderr=error_stream,
                start_new_session=True)
        scope.enter_context(patch.object(asr_client, 'SOCKET', str(asr_socket)))
        try:
            end = time.monotonic() + 10
            while not asr_socket.exists() and time.monotonic() < end and asr_process.returncode is None:
                await asyncio.sleep(.05)
            if not asr_socket.exists():
                raise RuntimeError('Fixture ASR socket did not start: ' + asr_log.read_text(errors='replace')[-600:])
            async with TestServer(fake) as peer:
                scope.enter_context(patch.object(C, 'bot_settings', return_value=(TOKEN, str(peer.make_url('/bot')))))
                async with TestClient(TestServer(server.create_app())) as client:
                    denied = await client.post('/api/auth', json={'initData': init_data(MEMBER + 1)})
                    assert denied.status == 403
                    node = shutil.which('node')
                    if not node: raise RuntimeError('Node runtime is required for the actual UI handler')
                    child = await asyncio.create_subprocess_exec(node, str(Path(__file__).with_name('upload_ui_example.cjs')),
                        str(client.make_url('/')), init_data(MEMBER), str(source), str(media / 'static/app.js'),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        env={k: v for k, v in clean.items() if k not in ['CREDENTIALS_DIRECTORY']})
                    try:
                        out, error = await asyncio.wait_for(child.communicate(), timeout=180)
                    finally:
                        if child.returncode is None: child.kill(); await child.wait()
                    if child.returncode: raise RuntimeError(error.decode(errors='replace')[-1200:])
                    ui = json.loads(out.splitlines()[-1])
                    with C.db() as db:
                        rows = [dict(r) for r in db.execute('SELECT * FROM jobs')]
                    assert len(rows) == 1
                    row = rows[0]
                    assert row['chat'] == GROUP and row['uid'] == MEMBER and row['state'] == 'queued'
                    assert row['offset'] == source.stat().st_size == row['size']
                    assert hashlib.sha256(C.file_path(row).read_bytes()).hexdigest() == ui['sha256'] == row['sha256']
                    assert C.file_path(row).stat().st_mode & 0o777 == 0o600
                    async with ClientSession(timeout=ClientTimeout(total=90)) as http:
                        claimed = worker.claim_next()
                        assert claimed['id'] == row['id'] and claimed['state'] == 'processing'
                        await asyncio.wait_for(worker.run_job(http, claimed), timeout=600)
                        with C.db() as db: pending = dict(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())
                        assert pending['state'] == 'delivery_pending'
                        await worker.run_job(http, pending)
                        # The same completed job must not cross the Telegram send boundary a second time.
                        with C.db() as db: final = dict(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())
                        await worker.deliver(http, final)
                    receipt = receipts.get(row['id'], body=True)
                    origin = json.loads(receipt['binding'])
                    assert grants.valid_job(origin) and origin['chat'] == GROUP and origin['uid'] == MEMBER
                    assert receipt['delivery'] == final['delivery'] == 'sent'
                    assert receipt['message_id'] == final['message_id'] == 9001
                    assert len(deliveries) == 1 and deliveries[0]['chat'] == GROUP
                    assert receipt['body'].decode() == deliveries[0]['text']
                    end = time.monotonic() + 5
                    while (asr_private / row['id']).exists() and time.monotonic() < end:
                        await asyncio.sleep(.05)
                    assert not (asr_private / row['id']).exists(), 'The receipt acknowledgement did not release the private ASR job'
                    text = deliveries[0]['text'].casefold()
                    references = {'green': 'green' in text, 'train': 'train' in text,
                                  'london': 'london' in text, 'nine': 'nine' in text or bool(__import__('re').search(r'\b9\b', text))}
                    if not all(references.values()):
                        raise RuntimeError('Synthetic speech reference gates failed: ' + json.dumps(references))
                    report = dict(ui, synthetic_member_non_owner=MEMBER != OWNER, synthetic_group=GROUP,
                        source_bytes=row['size'], input_integrity=True, upload_file_mode='600',
                        asr_mode='real_socket_service_and_installed_whisper', audio_duration_seconds=receipt['duration'],
                        speech_detected=True, transcript_bytes=len(receipt['body']), reference_words=references,
                        denied_nonmember=True, fake_group_documents_received=len(deliveries),
                        worker_owned_receipt='sent', projection_receipt='sent', receipt_message_id=9001,
                        duplicate_documents=0, membership_checks=len(membership_reads),
                        private_asr_snapshot_digest_checked=True, persisted_receipt_acknowledged=True,
                        fixture_state_only=True, external_network_blocked=True, production_telegram_calls=0)
                    if args.output:
                        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                        args.output.with_suffix('.txt').write_text(deliveries[0]['text'])
                    print(json.dumps(report, ensure_ascii=False))
        finally:
            broker.close()
            await broker.wait_closed()
            # On failure, the worker connection is already closed. Stop only fixture descendants.
            if asr_process.returncode is None:
                for pid in reversed(fixture_descendants(asr_process.pid)):
                    try: os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                asr_process.terminate()
                try: await asyncio.wait_for(asr_process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    asr_process.kill(); await asr_process.wait()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    repository = Path(__file__).resolve().parents[2]
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--media', type=Path, default=repository / 'media')
    parser.add_argument('--common', type=Path, default=repository / 'common' / 'psy_common.py')
    parser.add_argument('--model', type=Path, default=Path('/opt/psy-asr-model'))
    parser.add_argument('--asr-python', type=Path, default=Path('/opt/psy-asr-venv/bin/python'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main(args))
