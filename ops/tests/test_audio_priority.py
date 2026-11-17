import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import audio_priority
from audio_priority import decision

class Tests(unittest.TestCase):
    def test_active_recording_pauses_build_and_claims_only_its_own_freeze(self):
        state,action=decision({},True,'active','running',100)
        self.assertEqual(action,'freeze');self.assertTrue(state['managed_freeze'])
        state,action=decision(state,True,'active','frozen',120)
        self.assertIsNone(action);self.assertIsNone(state['idle_since'])
    def test_resumes_after_idle_grace_but_not_during_new_recording(self):
        state={'managed_freeze':True,'idle_since':None}
        state,action=decision(state,False,'active','frozen',100);self.assertIsNone(action)
        state,action=decision(state,False,'active','frozen',159);self.assertIsNone(action)
        state,action=decision(state,True,'active','frozen',160);self.assertIsNone(state['idle_since'])
        state,_=decision(state,False,'active','frozen',200)
        state,action=decision(state,False,'active','frozen',260)
        self.assertEqual(action,'thaw');self.assertFalse(state['managed_freeze'])
    def test_manual_freeze_is_not_thawed(self):
        state,action=decision({},True,'active','frozen',100);self.assertIsNone(action)
        state,action=decision(state,False,'active','frozen',1000);self.assertIsNone(action)
    def test_completed_or_missing_build_never_restarted(self):
        state,action=decision({'managed_freeze':True},False,'inactive','running',100)
        self.assertIsNone(action);self.assertFalse(state['managed_freeze'])
    def test_idle_wal_queue_read_from_read_only_folder_without_creating_files(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder);uploads=home/'vault/uploads';uploads.mkdir(parents=True)
            db=sqlite3.connect(uploads/'state.sqlite3');db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE jobs(id TEXT,state TEXT,updated INTEGER)');db.execute("INSERT INTO jobs VALUES('a','queued',0)")
            db.commit();db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.close()
            for side in ('-wal','-shm'):(uploads/('state.sqlite3'+side)).unlink(missing_ok=True)
            uploads.chmod(0o500)
            try:
                with patch.object(audio_priority,'HOME',home):self.assertIs(audio_priority.audio_busy(),True)
                self.assertEqual([p.name for p in uploads.iterdir()],['state.sqlite3'])
            finally:uploads.chmod(0o700)
    def test_unreadable_queue_changes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder);(home/'vault/uploads').mkdir(parents=True)
            os.mkfifo(home/'vault/uploads/state.sqlite3')
            with patch.object(audio_priority,'HOME',home):self.assertIsNone(audio_priority.audio_busy())

if __name__=='__main__':unittest.main()
