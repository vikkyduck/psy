from pathlib import Path
import sys
import tempfile
import unittest
from psy_modules import psy_group_store as store


class Pending(unittest.TestCase):
    def test_bound_dedup_recovery_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for i in range(1,5):
                self.assertEqual(store.pending(1,i,f"demo{i}",root=root),"queued")
            self.assertEqual(store.pending(1,1,"replacement",root=root),"existing")
            self.assertEqual(store.pending(1,11,"fifth from one member",root=root),"queued")
            self.assertEqual(store.pending(2,12,"other",root=root),"queued")
            self.assertEqual(store.claim_next(root=root),(1,"demo1"))
            self.assertEqual(store.recover(root=root),6)
            self.assertEqual(store.claim_next(root=root),(1,"demo1"))
            store.outbound(1,root=root)
            self.assertEqual(store.recover(root=root),5)
            self.assertEqual(store.claim_next(root=root),(2,"demo2"))
            with store.database(root) as c:
                self.assertEqual(c.execute("select state,body from requests where id=1").fetchone(),("uncertain",""))
            store.settle(2,True,root=root)
            self.assertEqual(store.claim_next(root=root),(3,"demo3"))
