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
        self.max = 1024**3
        self.reserve = storage.SYSTEM_RESERVE_BYTES
        self.free = 6 * 1024**3
        self.number = 0

    def job(self, size, offset, state, *, uid=1):
        self.number += 1
        self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(f'{self.number:032x}',uid,'.wav',size,offset,state))

    def capacity(self):
        with patch.object(storage.shutil,'disk_usage',return_value=SimpleNamespace(free=self.free)):
            return storage.capacity_snapshot(self.db,self.files,max_upload_bytes=self.max,reserve_bytes=self.reserve)

    def test_reserve_exceeds_one_maximum_upload_with_margin(self):
        self.assertGreaterEqual(storage.SYSTEM_RESERVE_BYTES, storage.MAX_UPLOAD_BYTES + 256 * 1024**2)

    def test_pending_counts_unreceived_bytes_results_and_one_asr_snapshot(self):
        text = storage.MAX_TRANSCRIPT_BYTES
        self.job(1000, 7, 'uploading')
        self.job(200, 200, 'processing', uid=2)
        self.job(900, 900, 'queued', uid=3)
        self.job(5000, 5000, 'done')
        self.job(700, 99, 'cancelled')
        c = self.capacity()
        # The decoder copies one recording at a time: the largest queued or running one.
        self.assertEqual(c['asr_reserved_bytes'], 900)
        self.assertEqual(c['pending_bytes'], 993 + 3*text + 900)
        self.assertEqual(c['disk_available_bytes'], self.free - self.reserve - c['pending_bytes'])
        self.assertFalse(any(key.startswith('member') for key in c))

    def test_new_upload_reserves_its_bytes_transcript_and_larger_snapshot(self):
        text = storage.MAX_TRANSCRIPT_BYTES
        self.job(500, 500, 'queued')
        self.free = self.reserve + text + 500 + (text + 300 + 0)
        c = self.capacity()
        self.assertIsNone(storage.admission_reason(c, 300, 300))
        self.assertEqual(storage.admission_reason(c, 301, 301), 'disk_capacity')
        # A repeat or retry adds no source bytes but needs its own snapshot if it is the largest.
        self.assertIsNone(storage.admission_reason(c, 0, 800))
        self.assertEqual(storage.admission_reason(c, 0, 801), 'disk_capacity')

    def test_low_space_flag_matches_one_maximum_upload(self):
        text = storage.MAX_TRANSCRIPT_BYTES
        self.free = self.reserve + 2*self.max + text
        self.assertFalse(self.capacity()['disk_low'])
        self.assertIsNone(storage.admission_reason(self.capacity(), self.max, self.max))
        self.free -= 1
        self.assertTrue(self.capacity()['disk_low'])
        self.assertEqual(storage.admission_reason(self.capacity(), self.max, self.max), 'disk_capacity')

    def test_many_recordings_of_one_member_are_admitted_while_space_lasts(self):
        for _ in range(40):
            self.assertIsNone(storage.admission_reason(self.capacity(), 10, 10))
            self.job(10, 0, 'uploading')
        self.assertEqual(self.capacity()['pending_bytes'], 400 + 40*storage.MAX_TRANSCRIPT_BYTES)

    def test_invalid_requests_fail_closed(self):
        with self.assertRaises(ValueError):
            storage.admission_reason(self.capacity(), -1, 0)
        with self.assertRaises(ValueError):
            storage.admission_reason(self.capacity(), 1, True)


if __name__=='__main__':
    unittest.main()
