"""Exercise a real zero-progress decoder, plus deterministic progress and queue semantics."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

with patch.dict(os.environ, PSY_ASR_INPUT='/unused', PSY_ASR_OUTPUT='/unused'):
    import asr_service as asr
import asr_client
import psy_upload_common as C
import queue_health
import worker


class LocalProgress(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dir = self.root / 'job'
        self.dir.mkdir()
        self.public = self.root / 'numeric'
        self.public.mkdir()
        self.ident = 'a'*32
        self.dest = self.public / (self.ident+'.json')
        self.source = self.dir / 'progress.json'

    def write(self, done):
        self.source.write_text(json.dumps({'processed_seconds':done, 'total_seconds':600}))

    def test_only_advances_refresh_mtime_and_reset_age(self):
        self.write(60)
        high = asr.publish_progress(self.dir, self.dest, 0)
        self.assertEqual(high,60)
        start = int(time.time()) - 4000
        observed = start+100
        os.utime(self.dest,(observed,observed))
        for nonprogress in (60,30,0,601,True):
            self.write(nonprogress)
            self.assertEqual(asr.publish_progress(self.dir,self.dest,high),60)
            self.assertEqual(self.dest.stat().st_mtime,observed)
        self.assertEqual(queue_health.processing_age(start,self.ident,self.public,observed+2100),2100)
        self.write(61)
        self.assertEqual(asr.publish_progress(self.dir,self.dest,high),61)
        self.assertLessEqual(queue_health.processing_age(start,self.ident,self.public),1)
        self.assertEqual(set(json.loads(self.dest.read_text())), {'processed_seconds','total_seconds'})

    def test_no_progress_or_stale_retry_uses_new_start(self):
        now = int(time.time())
        for kind in ('no file','old attempt','future','symlink'):
            if kind=='old attempt':
                self.dest.write_text('{}')
                os.utime(self.dest,(now-5000,now-5000))
            if kind=='future':
                os.utime(self.dest,(now+120,now+120))
            if kind=='symlink':
                self.dest.unlink()
                self.source.touch()
                self.dest.symlink_to(self.source)
            self.assertEqual(queue_health.processing_age(now-100,self.ident,self.public,now),100)
        db = self.root/'jobs.sqlite'
        with sqlite3.connect(db) as c:
            c.execute('CREATE TABLE jobs(id text, state text, delivery text, updated integer,size int,offset int)')
            c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(self.ident,'processing',None,now-2200,500,500))
        with patch('shutil.disk_usage') as usage:
            usage.return_value.free=C.RESERVE+10
            report = queue_health.snapshot(db,now,self.public,files=self.dir)
        self.assertEqual(report['oldest_processing_without_progress'],2200)
        self.assertGreaterEqual(report['oldest_processing_without_progress'],queue_health.PROGRESS_WARNING_SECONDS)
        self.assertTrue(report['progress_warning'])
        self.assertTrue(report['disk_low'])
        self.assertNotIn(self.ident,json.dumps(report))
        self.assertFalse(any(key.startswith('member') for key in report))

    def test_stall_kills_process_group_and_preserves_checkpoint(self):
        # The stale marker must not give an old attempt artificial progress on restart.
        self.write(50)
        self.dest.write_text(self.source.read_text())
        checkpoint=self.dir/'checkpoint.json'
        checkpoint.write_text('unchanged synthetic checkpoint')
        grandchild = self.dir/'descendant'
        cmd = [sys.executable,'-c',"""
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable,'-c',"import time;time.sleep(60)"])
with open(sys.argv[1],'w') as f: f.write(str(child.pid))
time.sleep(60)
""",str(grandchild)]
        spawn = subprocess.Popen
        def fixture(args, **kwargs):
            return spawn(cmd,**kwargs)
        env = {**os.environ,'PSY_ASR_PROGRESS':str(self.public)}
        with patch.object(asr.subprocess,'Popen',side_effect=fixture), \
             patch.object(asr,'POLL_SECONDS',0.05),patch.object(asr,'STALL_TIMEOUT',0.5):
            with self.assertRaises(asr.ASRStalled):
                asr.decode(self.dir,Path('unused'),Path('unused'),'auto',self.ident,env)
        self.assertEqual(checkpoint.read_text(),'unchanged synthetic checkpoint')
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.source.exists())
        pid = int(grandchild.read_text())
        end = time.monotonic()+2
        while time.monotonic()<end:
            status = subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True)
            if status.returncode or status.stdout.strip().startswith('Z'):
                break
            time.sleep(0.02)
        else:
            self.fail('descendant still running after watchdog timeout')

    def test_decode_advances_then_finishes(self):
        self.write(500)
        self.dest.write_text(self.source.read_text())
        cmd=[sys.executable,'-c',"""
import json,os,sys,time
from pathlib import Path
p=Path(sys.argv[1]); assert not p.exists()
for done in [10,20,30,40]:
    time.sleep(0.16)
    tmp=p.with_suffix('.part');tmp.write_text(json.dumps(dict(processed_seconds=done,total_seconds=40)))
    tmp.replace(p)
print(json.dumps(dict(complete=True,duration=40)))
""",str(self.source)]
        spawn = subprocess.Popen
        with patch.object(asr.subprocess,'Popen',side_effect=lambda args,**kw:spawn(cmd,**kw)), \
             patch.object(asr,'POLL_SECONDS',0.03),patch.object(asr,'STALL_TIMEOUT',0.45):
            result = asr.decode(self.dir,Path('unused'),Path('unused'),'auto',self.ident,
                                {**os.environ,'PSY_ASR_PROGRESS':str(self.public)})
        self.assertTrue(result['complete'])
        self.assertGreaterEqual(json.loads(self.dest.read_text())['processed_seconds'],30)


class StalledJobContract(unittest.IsolatedAsyncioTestCase):
    async def test_wire_reason_persists_once_for_manual_retry(self):
        reader=asyncio.StreamReader()
        reader.feed_data(b'{"complete":false,"error":"stalled"}\n')
        reader.feed_eof()
        writer=AsyncMock()
        from unittest.mock import Mock
        writer.write=Mock();writer.close=Mock()
        with tempfile.TemporaryDirectory() as temp, \
             patch.multiple(C,ROOT=Path(temp)/'db',FILES=Path(temp)/'files'):
            C.initialize()
            with C.db() as c:
                c.execute("""INSERT INTO jobs(id,uid,chat,filename,ext,size,fingerprint,language,state,created,updated)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?)""",('b'*32,C.OWNER,C.OWNER,'test.wav','.wav',1,'fake','ru','processing',0,0))
                job=dict(c.execute('SELECT * FROM jobs').fetchone())
            C.file_path(job).write_bytes(b'X')
            with patch.object(asyncio,'open_unix_connection',AsyncMock(return_value=(reader,writer))):
                await worker.run_job(None,job)
            with C.db() as c:
                row=c.execute('SELECT state,error,attempts FROM jobs').fetchone()
                self.assertEqual(row['state'],'failed')
                self.assertEqual(row['attempts'],0)
                self.assertIn('45 минут', row['error'])
                self.assertIsNone(worker.next_ready_job(c))

if __name__=='__main__':
    unittest.main()
