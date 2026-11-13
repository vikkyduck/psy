import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import psy_upload_common as C
import worker
import recording_receipts as receipts
import recording_grant as grants


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths=patch.multiple(C,ROOT=Path(tmp.name)/'state',FILES=Path(tmp.name)/'files')
        paths.start()
        self.addCleanup(paths.stop)
        env=patch.dict(os.environ,PSY_RECEIPT_ROOT=tmp.name)
        env.start()
        self.addCleanup(env.stop)
        receipts.initialize()
        C.initialize()
        self.ident='a'*32
        self.ok={'ok':True,'result':{'status':'member'}}
        with C.db() as c:
            c.execute("""INSERT INTO jobs(id,uid,chat,filename,ext,size,fingerprint,language,state,
                      created,updated,speech_detected) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (self.ident,17,C.GROUP,'example.wav','.wav',10,'x','ru','delivery_pending',0,0,0))
        job=self.job()
        job['proof']=grants.sign('job',grants.job_fields(job))
        receipts.save(job,{'duration':1,'speech_detected':False},b'fixture')
        with C.db() as c:
            c.execute('UPDATE jobs SET proof=?',(job['proof'],))

    def job(self):
        with C.db() as c:
            return dict(c.execute('SELECT * FROM jobs').fetchone())

    async def test_membership_unavailable_does_not_revoke_or_send(self):
        with patch.object(worker,'bot_call',AsyncMock(side_effect=TimeoutError())) as api:
            await worker.run_job(None,self.job())
        self.assertEqual(api.await_count,1)
        self.assertEqual(self.job()['state'],'delivery_pending')
        self.assertGreater(self.job()['next_try'],C.now())
        with patch.object(worker,'bot_call',AsyncMock(return_value={'ok':True,'result':{'status':'left'}})):
            await worker.run_job(None,self.job())
        self.assertEqual(self.job()['delivery'],'access_revoked')

    async def test_429_retries_only_delivery_and_records_receipt(self):
        rejected={'ok':False,'error_code':429,'parameters':{'retry_after':45}}
        with patch.object(worker,'bot_call',AsyncMock(side_effect=[self.ok,rejected])):
            await worker.run_job(None,self.job())
        self.assertEqual(self.job()['state'],'delivery_pending')
        self.assertGreaterEqual(self.job()['next_try'],C.now()+44)
        with patch.object(C,'now',return_value=C.now()+60), patch.object(worker,'bot_call',AsyncMock(side_effect=[self.ok,{'ok':True,'result':{'message_id':12}}])):
            await worker.run_job(None,self.job())
        self.assertEqual((self.job()['state'],self.job()['delivery'],self.job()['message_id']),('done','sent',12))

    async def test_ambiguous_send_is_not_replayed(self):
        with patch.object(worker,'bot_call',AsyncMock(side_effect=[self.ok,TimeoutError()])):
            await worker.run_job(None,self.job())
        self.assertEqual((self.job()['state'],self.job()['delivery']),('done','unknown'))


class AsrTests(unittest.TestCase):
    def test_no_duration_webm_uses_bounded_audio_fallback_and_silence_is_labelled(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for name in ('in','out','model'):
                (root/name).mkdir()
            src=root/'in'/'clip.webm'
            src.write_bytes(b'fixture')
            output=root/'out'/'result.txt'
            conditions={'PSY_ASR_STANDALONE':'1','PSY_ASR_INPUT':str(src.parent),
                        'PSY_ASR_OUTPUT':str(output.parent),'PSY_WHISPER_MODEL':str(root/'model'),
                        'HERMES_HOME':td,'PSY_ASR_PROGRESS_FILE':str(root/'progress.json')}
            whisper=types.ModuleType('faster_whisper')
            whisper.WhisperModel=Mock()
            whisper.WhisperModel.return_value.transcribe.return_value=([],None)
            with patch.dict(os.environ,conditions),patch.dict(sys.modules,{'faster_whisper':whisper}):
                env=runpy.run_path(str(Path(__file__).resolve().parent.parent/'transcribe.py'))
                main=env['main']
                # Decoder/VAD are a separate accuracy suite; this fixture supplies empty speech windows.
                with patch.dict(main.__globals__,{'find_program':lambda x:x,'read_window':lambda *_:b'',
                        'speech_ranges':lambda _audio:[]}),patch.object(sys,'argv',
                        ['asr',str(src),'--output',str(output)]),patch('subprocess.check_output',
                        return_value=b'{"streams":[{}],"format":{}}'),patch('subprocess.run',
                        return_value=types.SimpleNamespace(stdout=b'out_time_us=61000000\n')):
                    answer=io.StringIO()
                    with contextlib.redirect_stdout(answer):
                        main()
            result=json.loads(answer.getvalue())
            self.assertTrue(result['complete'])
            self.assertFalse(result['speech_detected'])
            self.assertEqual(result['chunks'],2)
            self.assertIn('речь',output.read_text())
            self.assertEqual(json.loads((root/'progress.json').read_text()),{'processed_seconds':61,'total_seconds':61})
