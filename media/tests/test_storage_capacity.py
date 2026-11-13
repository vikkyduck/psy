import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import storage_capacity as storage


class CapacityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.files = Path(self.temp.name)
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE jobs(id TEXT,uid INTEGER,ext TEXT,size INTEGER,offset INTEGER,state TEXT)')
        self.limit = 4 * 1024**3
        self.max = 1024**3
        self.reserve = 768 * 1024**2
        self.free = 6 * 1024**3

    def job(self, number, size, offset, state, *, uid=1):
        ident = f'{number:032x}'
        self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(ident,uid,'.wav',size,offset,state))
        return self.files / (ident+'.wav')

    def capacity(self, uid=1):
        with patch.object(storage.shutil,'disk_usage',return_value=SimpleNamespace(free=self.free)):
            return storage.capacity_snapshot(self.db,self.files,uid,max_upload_bytes=self.max,
                                             reserve_bytes=self.reserve,quota_bytes=self.limit)

    def test_physical_inode_and_each_result(self):
        first=self.job(1,100,100,'done')
        first.write_bytes(bytes(100))
        first.with_suffix('.txt').write_bytes(bytes(5))
        second=self.job(2,100,100,'done')
        os.link(first,second)
        second.with_suffix('.txt').write_bytes(bytes(9))
        third=self.job(3,100,100,'done')
        third.write_bytes(bytes(100))
        other=self.job(4,300,300,'done',uid=2)
        other.write_bytes(bytes(300))
        self.assertEqual(self.capacity()['member_used_bytes'],214)
        self.assertEqual(self.capacity(2)['member_used_bytes'],300)

    def test_reserve_drafts_and_future_results_for_all_users(self):
        first=self.job(1,1000,7,'uploading')
        first.write_bytes(bytes(7))
        self.job(2,200,200,'processing',uid=2).write_bytes(bytes(200))
        self.job(3,700,99,'cancelled')
        c=self.capacity()
        self.assertEqual(c['member_used_bytes'],7)
        self.assertEqual(c['member_reserved_bytes'],993+storage.MAX_TRANSCRIPT_BYTES)
        self.assertEqual(c['pending_bytes'],993+2*storage.MAX_TRANSCRIPT_BYTES)
        self.assertEqual(c['disk_available_bytes'],self.free-self.reserve-c['pending_bytes'])
        self.assertIsNone(self.capacity(None)['member_used_bytes'])

    def test_threshold_and_overquota_legacy_retries(self):
        text=storage.MAX_TRANSCRIPT_BYTES
        self.job(1,42,42,'done').write_bytes(bytes(42))
        self.limit=self.max+text+42
        self.assertIsNone(storage.admission_reason(self.capacity(),self.max))
        self.assertFalse(self.capacity()['member_low'])
        self.limit -= 1
        self.assertEqual(storage.admission_reason(self.capacity(),self.max),'member_quota')
        self.assertTrue(self.capacity()['member_low'])
        self.limit=23
        self.assertEqual(self.capacity()['member_available_bytes'],0)
        self.assertEqual(storage.admission_reason(self.capacity(),0),'member_quota')
        self.assertIsNone(storage.admission_reason(self.capacity(),0,retry=True))
        self.free=self.reserve+text-1
        self.assertEqual(storage.admission_reason(self.capacity(),0,retry=True),'disk_capacity')
        self.assertEqual(next(self.files.iterdir()).stat().st_size,42)

    def test_system_threshold_excludes_margin_and_other_members_reservations(self):
        text=storage.MAX_TRANSCRIPT_BYTES
        self.job(1,900,0,'uploading',uid=2)
        self.free = self.reserve + self.max + 2*text + 900
        self.assertFalse(self.capacity(None)['disk_low'])
        self.assertIsNone(storage.admission_reason(self.capacity(), self.max))
        self.free -= 1
        self.assertTrue(self.capacity(None)['disk_low'])
        self.assertEqual(storage.admission_reason(self.capacity(),self.max),'disk_capacity')

    def test_cancelled_residue_charged_until_physically_removed(self):
        source=self.job(1,100,25,'cancelled')
        source.write_bytes(bytes(25))
        self.assertEqual(self.capacity()['member_used_bytes'],25)
        self.assertEqual(self.capacity()['member_reserved_bytes'],0)
        source.unlink()
        self.assertEqual(self.capacity()['member_used_bytes'],0)

    def test_unsafe_path_fails_closed(self):
        source=self.job(1,40,40,'done')
        source.symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):
            self.capacity()


if __name__=='__main__':
    unittest.main()
