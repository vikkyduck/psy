"""Attempt transcript, recipient and source substitution on the normal worker entry point."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import access_broker as broker
import psy_upload_common as C
import recording_grant as grants
import recording_receipts as receipts
import worker


class ResultBoundary(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        private=root/'private'
        private.mkdir()
        for scope in (patch.dict(os.environ,PSY_RECEIPT_ROOT=str(private)),patch.multiple(C,ROOT=root/'state',FILES=root/'files')):
            scope.start()
            self.addCleanup(scope.stop)
        C.initialize()
        receipts.initialize()
        self.session=grants.session('fixture',{'uid':C.OWNER,'chat':C.OWNER},C.now()+300)
        self.job=grants.bind(self.session,dict(filename='record.wav',ext='.wav',size=1,language='ru',content_id='0'*64))
        self.job.update(state='delivery_pending',attempts=0)
        with C.db() as db:
            db.execute("""INSERT INTO jobs(id,uid,chat,filename,ext,size,language,content_id,fingerprint,proof,state,created,updated)
                VALUES(:id,:uid,:chat,:filename,:ext,:size,:language,:content_id,'x',:proof,:state,0,0)""",self.job)
        canary=root/'canary'
        canary.write_bytes(b'private fixture')
        C.file_path(self.job,'.txt').symlink_to(canary)
        self.sends=[]

    async def sink(self,http,method,payload):
        self.assertEqual(method,'sendDocument')
        self.sends.append({field[0]['name']:field[2].read() if isinstance(field[2],io.BytesIO) else field[2] for field in payload._fields})
        return {'ok':True,'result':{'message_id':1}}

    async def test_forged_pending_symlink_and_regular_file_are_never_opened(self):
        with patch.object(worker,'bot_call',self.sink):
            await worker.run_job(None,self.job)
            path=C.file_path(self.job,'.txt')
            path.unlink()
            path.write_bytes(b'planted document')
            await worker.run_job(None,self.job)
        self.assertEqual(self.sends,[])

    async def test_only_asr_result_to_verified_route_once_even_if_db_replayed(self):
        receipts.save(self.job,dict(duration=1,speech_detected=True),b'actual ASR')
        with patch.object(worker,'bot_call',self.sink):
            for forged in ({'chat':C.GROUP},{'uid':762},{'thread':4},{'reply':5}):
                await worker.run_job(None,{**self.job,**forged})
            self.assertEqual(self.sends,[])
            await worker.run_job(None,{**self.job,'filename':'forged field'})
            await worker.run_job(None,self.job)
        self.assertEqual(len(self.sends),1)
        self.assertEqual(self.sends[0]['document'],b'actual ASR')
        self.assertEqual(self.sends[0]['chat_id'],str(C.OWNER))
        self.assertIn('record.wav',self.sends[0]['caption'])

    async def test_forged_queue_and_broker_context_rejected_before_asr(self):
        with patch.object(worker,'transcribe',AsyncMock()) as asr,patch.object(worker,'bot_call',self.sink):
            await worker.run_job(None,{**self.job,'state':'queued','chat':C.GROUP})
            await worker.run_job(None,{**self.job,'state':'queued','content_id':'2'*64})
            asr.assert_not_awaited()
        self.assertEqual(self.sends,[])
        request=dict(action='bind',session={**self.session,'chat':C.GROUP},secret='fixture',shape=self.job)
        self.assertEqual(broker.dispatch(request,'synthetic')['status'],403)

    def test_decoder_copies_checked_bytes_to_private_file_and_rejects_links(self):
        import importlib
        with patch.dict(os.environ,PSY_ASR_INPUT=str(C.FILES),PSY_ASR_OUTPUT=str(receipts.root())):
            asr=importlib.import_module('asr_service')
        data=b'fixture bytes'
        row={**self.job,'size':len(data),'content_id':hashlib.sha256(hashlib.sha256(data).digest()).hexdigest()}
        source=C.file_path(row)
        source.write_bytes(data)
        with patch.object(asr,'INPUT',C.FILES):
            bound,digest=asr.snapshot(receipts.root(),row)
            source.write_bytes(b'other content')
            self.assertEqual(bound.read_bytes(),data)
            self.assertEqual(digest,hashlib.sha256(data).hexdigest())
            with self.assertRaises(ValueError):
                asr.snapshot(receipts.root(),row)
            source.unlink()
            source.symlink_to(bound)
            with self.assertRaises(OSError):
                asr.snapshot(receipts.root(),row)
