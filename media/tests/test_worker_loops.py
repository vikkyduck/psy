"""Member turns, independent transcription and delivery loops, and resumable time slots."""
import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import psy_upload_common as C
import recording_grant as grants
import recording_receipts as receipts
import worker


class WorkerLoops(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / 'receipts').mkdir()
        for scope in (patch.multiple(C, ROOT=root/'state', FILES=root/'files'),
                      patch.dict(os.environ, PSY_RECEIPT_ROOT=str(root/'receipts')),
                      patch.object(worker, 'POLL_SECONDS', 0.01), patch.object(worker, 'WATCH_SECONDS', 0.02)):
            scope.start()
            self.addCleanup(scope.stop)
        C.initialize()
        receipts.initialize()
        self.session = grants.session('fixture', {'uid': C.OWNER, 'chat': C.OWNER}, C.now() + 300)
        self.sends = []

    def add(self, state, uid=None, queued=0, name='take.wav'):
        session = self.session if uid is None else grants.session('fixture', {'uid': uid, 'chat': C.GROUP}, C.now()+300)
        job = grants.bind(session, dict(filename=f'{queued}-{name}', ext='.wav', size=1, language='ru',
                                        content_id='0'*64))
        with C.db() as db:
            db.execute("""INSERT INTO jobs(id,uid,chat,thread,reply,filename,ext,size,language,content_id,fingerprint,
                          proof,state,created,updated,queued_at_ns) VALUES(:id,:uid,:chat,:thread,:reply,:filename,
                          :ext,:size,:language,:content_id,'x',:proof,:state,1,1,:queued)""",
                       {**job, 'state': state, 'queued': queued})
        return job['id']

    def row(self, ident):
        with C.db() as db:
            return dict(db.execute('SELECT * FROM jobs WHERE id=?', (ident,)).fetchone())

    async def sink(self, http, method, payload):
        if method == 'getChatMember':
            return {'ok': True, 'result': {'status': 'member'}}
        self.sends.append({f[0]['name']: f[2].read() if isinstance(f[2], io.BytesIO) else f[2] for f in payload._fields})
        return {'ok': True, 'result': {'message_id': 40 + len(self.sends)}}

    def test_members_take_turns_on_the_decoder(self):
        first = self.add('queued', uid=71, queued=1)
        second = self.add('queued', uid=71, queued=2)
        third = self.add('queued', uid=72, queued=3)
        self.assertEqual(worker.claim_next()['id'], first)
        # Member 72 has not had a turn yet, so it goes before member 71's second recording.
        self.assertEqual(worker.claim_next()['id'], third)
        self.assertEqual(worker.claim_next()['id'], second)
        self.assertIsNone(worker.claim_next())
        self.assertEqual(self.row(first)['state'], 'processing')
        self.assertIsNotNone(self.row(first)['started_at'])

    async def test_delivery_and_redelivery_continue_during_a_long_decode(self):
        decoding = self.add('queued', queued=1, name='long.wav')
        ready = self.add('delivery_pending', name='ready.wav')
        record = self.row(ready)
        receipts.save(record, dict(duration=1, speech_detected=True), b'finished text')
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(job):
            started.set()
            await release.wait()
        with patch.object(worker, 'transcribe', slow), patch.object(worker, 'bot_call', self.sink):
            loops = [asyncio.ensure_future(worker.transcription_loop(None)),
                     asyncio.ensure_future(worker.delivery_loop(None))]
            try:
                await asyncio.wait_for(started.wait(), 2)
                for _ in range(200):
                    if self.row(ready)['delivery'] == 'sent':
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual((self.row(ready)['state'], self.row(ready)['delivery']), ('done', 'sent'))
                self.assertEqual(self.sends[0]['document'], b'finished text')
                self.assertEqual(self.row(decoding)['state'], 'processing')
                self.assertFalse(release.is_set())
            finally:
                for loop in loops:
                    loop.cancel()
                await asyncio.gather(*loops, return_exceptions=True)

    async def test_deleting_a_recording_cancels_its_running_decode(self):
        ident = self.add('queued')
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def endless(job):
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        with patch.object(worker, 'transcribe', endless):
            job = worker.claim_next()
            task = asyncio.ensure_future(worker.supervise(None, job))
            await asyncio.wait_for(started.wait(), 2)
            with C.db() as db:
                self.assertTrue(C.transition(db, ident, 'processing', 'deleted'))
            await asyncio.wait_for(task, 2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.row(ident)['state'], 'deleted')

    async def test_time_slot_requeues_without_error_and_resumes(self):
        ident = self.add('queued')
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"complete":false,"error":"deadline"}\n')
        reader.feed_eof()
        writer = AsyncMock()
        writer.write = Mock()
        writer.close = Mock()
        job = worker.claim_next()
        with patch.object(asyncio, 'open_unix_connection', AsyncMock(return_value=(reader, writer))):
            await worker.run_job(None, job)
        row = self.row(ident)
        self.assertEqual((row['state'], row['error'], row['attempts'], row['next_try']), ('queued', None, 0, 0))
        self.assertEqual(worker.claim_next()['id'], ident)

    async def test_result_is_acknowledged_only_after_the_receipt_is_saved(self):
        ident = self.add('queued')
        text = 'итоговый текст'.encode()
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps({'complete': True, 'duration': 3.0, 'speech_detected': True,
                                     'text_bytes': len(text), 'sha256': 'f'*64}).encode() + b'\n' + text)
        reader.feed_eof()
        writer = AsyncMock()
        acks = []
        def write(data):
            acks.append((data, receipts.get(ident) is not None))
        writer.write = Mock(side_effect=write)
        writer.close = Mock()
        job = worker.claim_next()
        with patch.object(asyncio, 'open_unix_connection', AsyncMock(return_value=(reader, writer))):
            await worker.run_job(None, job)
        self.assertEqual(acks[-1], (b'{"saved":true}\n', True))
        self.assertEqual(self.row(ident)['state'], 'delivery_pending')
        self.assertEqual(receipts.get(ident, body=True)['body'], text)


    async def test_interrupted_send_is_settled_inside_the_loop_and_can_be_confirmed_again(self):
        unknown = self.add('delivery_pending', queued=1)
        claimed = self.add('delivery_pending', queued=2)
        failed = self.add('delivery_pending', queued=3)
        for ident in (unknown, claimed, failed):
            receipts.save(self.row(ident), dict(duration=1, speech_detected=True), b'text ' + ident.encode())
        with C.db() as db:
            for ident in (unknown, claimed):
                self.assertTrue(C.transition(db, ident, 'delivery_pending', 'sending'))
            self.assertTrue(C.transition(db, failed, 'delivery_pending', 'done', delivery='failed'))
        # A crash or exception after marking the receipt, without restarting the worker.
        self.assertTrue(receipts.start(unknown, C.now()))
        self.assertTrue(receipts.start(failed, C.now()))
        with patch.object(worker, 'bot_call', self.sink):
            self.assertTrue(await worker.delivery_step(None))
        self.assertEqual((self.row(unknown)['state'], self.row(unknown)['delivery']), ('done', 'unknown'))
        self.assertEqual(receipts.get(unknown)['delivery'], 'unknown')
        self.assertEqual(receipts.get(failed)['delivery'], 'unknown')
        # Nothing reached Telegram for the claimed one, so it was delivered normally.
        self.assertEqual((self.row(claimed)['state'], self.row(claimed)['delivery']), ('done', 'sent'))
        self.assertEqual([send['document'] for send in self.sends], [b'text ' + claimed.encode()])
        # "Повторить отправку" with the duplicate confirmation works without a worker restart.
        proof = grants.redelivery(self.session, self.row(unknown), True)
        with C.db() as db:
            db.execute("UPDATE jobs SET state='delivery_pending',delivery=NULL,retry_proof=? WHERE id=?",
                       (json.dumps(proof), unknown))
        with patch.object(worker, 'bot_call', self.sink):
            self.assertTrue(await worker.delivery_step(None))
        self.assertEqual((self.row(unknown)['state'], self.row(unknown)['delivery']), ('done', 'sent'))
        self.assertEqual(len(self.sends), 2)

if __name__ == '__main__':
    unittest.main()
