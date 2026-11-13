"""Crash a real Hermes renewal on either side of its commit, using only synthetic accounts."""
import argparse
import base64
from contextlib import ExitStack
import json
import multiprocessing
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_renewal import deny_network, test_token


class DurableRenewal(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.home = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        stack.enter_context(patch.dict(os.environ, {"HOME": str(self.home), "HERMES_HOME": str(self.home)}))
        self.assertEqual(auth._auth_file_path().parent.resolve(), self.home)
        stack.enter_context(patch.object(policy, "MARKERS", self.home))
        self.old = dict(id="primary", source="manual:device_code", auth_type="oauth", priority=0,
                        access_token=test_token(time.time() - 10, "A"), refresh_token="fixture-old")
        alias = dict(self.old, id="alias", priority=1)
        self.other = dict(self.old, id="independent", priority=2, refresh_token="fixture-independent",
                          access_token=test_token(time.time() + 90000, "B"))
        auth._save_auth_store({"credential_pool": {policy.PROVIDER: [self.old, alias, self.other]}})
        self.log = self.home / "attempts"
        self.next_pair = dict(access_token=test_token(time.time() + 172800, "A"),
                              refresh_token="fixture-new", last_refresh="fixture")
        self.receipt = self.home / "psy-codex-received.json"
        self.journal = self.home / "psy-codex-renewals" / (policy.fingerprint(self.old["refresh_token"]) + ".json")
        stack.enter_context(patch.object(provider, "_exchange_codex_token", side_effect=self.exchange))

    def exchange(self, grant, timeout):
        self.assertEqual(grant, self.old["refresh_token"])
        with self.log.open("a") as fd:
            fd.write("attempt\n")
        return self.next_pair.copy()

    def crash(self, phase):
        def child():
            # os._exit releases OS locks without completing finally blocks.
            boundary = "_save_store" if phase == "received" else "_finish_receipt"
            with patch.object(policy, boundary, side_effect=lambda *args: os._exit(41)):
                policy.model_credentials()
        process = multiprocessing.get_context("fork").Process(target=child)
        process.start()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join(3)
        self.assertEqual(process.exitcode, 41)
        self.assertEqual(stat.S_IMODE(self.receipt.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.receipt.parent.stat().st_mode), 0o700)
        self.assertEqual(json.loads(self.journal.read_bytes())["state"], "pending")

    def verify_recovery(self):
        self.assertEqual(policy.model_credentials()["api_key"], self.next_pair["access_token"])
        self.assertFalse(self.receipt.exists())
        rows = auth._load_auth_store()["credential_pool"][policy.PROVIDER]
        for row in rows[:2]:
            self.assertEqual((row["access_token"], row["refresh_token"]),
                             (self.next_pair["access_token"], self.next_pair["refresh_token"]))
        self.assertEqual(rows[2], self.other)
        self.assertEqual(self.log.read_text().splitlines(), ["attempt"])

    def test_exit_after_received(self):
        self.crash("received")
        self.verify_recovery()
        self.assertEqual(json.loads(self.journal.read_bytes()), {"state": "spent"})

    def test_exit_after_auth_store_commit(self):
        self.crash("committed")
        self.verify_recovery()
        self.assertEqual(json.loads(self.journal.read_bytes()), {"state": "spent"})

    def test_same_grant_and_different_bearer(self):
        self.next_pair["refresh_token"] = self.old["refresh_token"]
        self.crash("received")
        self.verify_recovery()
        self.assertFalse(self.journal.exists())

    def test_new_owner_login_wins_and_new_attempt_keeps_its_barrier(self):
        self.next_pair["refresh_token"] = self.old["refresh_token"]
        self.crash("received")
        store = auth._load_auth_store()
        new_access = test_token(time.time() + 300000, "A")
        store["credential_pool"][policy.PROVIDER][0].update(access_token=new_access, refresh_token="owner-confirmed")
        auth._save_auth_store(store)
        newer = {"state": "pending", "attempt": "different-exchange"}
        auth._save_private_json(self.journal, newer, fsync_dir=True)
        self.assertEqual(policy.model_credentials()["api_key"], new_access)
        self.assertFalse(self.receipt.exists())
        self.assertEqual(json.loads(self.journal.read_bytes()), newer)
        self.assertEqual(self.log.read_text().splitlines(), ["attempt"])

    def test_native_alias_id_runs_one_shared_commit(self):
        alias = auth._load_auth_store()["credential_pool"][policy.PROVIDER][1]
        renewed, _, changed = policy.renew_native(alias)
        self.assertTrue(changed)
        self.assertEqual(renewed["id"], "alias")
        self.verify_recovery()

    def test_owner_identity_requires_both_the_same_individual_and_workspace(self):
        def bearer(sub, user, workspace):
            details = {"chatgpt_user_id": user, "chatgpt_account_id": workspace}
            claims = {"exp": time.time() + 86400,
                      "https://api.openai.com/auth": {key: val for key, val in details.items() if val is not None}}
            if sub is not None:
                claims["sub"] = sub
            payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            return "e30." + payload + ".fixture"

        for old, new in (
            ((None, None, "team"), ("B", None, "team")),
            ((None, "A", "team"), (None, "B", "team")),
            (("A", "U1", "team"), ("A", "U2", "team")),
            (("A", None, "first"), ("A", None, "second")),
            (("A", None, None), ("A", None, "second")),
            (("A", None, "first"), ("A", None, None)),
        ):
            with self.subTest(old=old, new=new):
                store = auth._load_auth_store()
                rows = store["credential_pool"][policy.PROVIDER]
                for row in rows[:2]:
                    row["access_token"] = bearer(*old)
                auth._save_auth_store(store)
                with self.assertRaises(policy.CredentialIssue):
                    policy.install_owner_login(policy.generation(rows[0]),
                                               dict(self.next_pair, access_token=bearer(*new)))
                self.assertEqual(auth._load_auth_store()["credential_pool"][policy.PROVIDER], rows)
        # Some tokens use the explicit ChatGPT user claim instead of sub.
        store = auth._load_auth_store()
        rows = store["credential_pool"][policy.PROVIDER]
        for row in rows[:2]:
            row["access_token"] = bearer(None, "A", "team")
        auth._save_auth_store(store)
        expected = bearer(None, "A", "team")
        policy.install_owner_login(policy.generation(rows[0]), dict(self.next_pair, access_token=expected))
        self.assertEqual(policy.model_credentials()["api_key"], expected)

    def test_device_install_checks_owner_and_compares_generations(self):
        store = auth._load_auth_store()
        rows = store["credential_pool"][policy.PROVIDER]
        for row in rows[:2]:
            row.update(last_status="dead", last_error_reason="invalid_grant")
        auth._save_auth_store(store)
        observed = policy.generation(self.old)
        stranger = dict(self.next_pair, access_token=test_token(time.time()+3600, "B"))
        with self.assertRaises(policy.CredentialIssue):
            policy.install_owner_login(observed, stranger)
        self.assertEqual(auth._load_auth_store()["credential_pool"][policy.PROVIDER], rows)
        installed = policy.install_owner_login(observed, self.next_pair)
        self.assertEqual(policy.install_owner_login(observed, stranger), installed)
        self.assertEqual(policy.model_credentials()["generation"], installed)
        self.assertFalse(self.log.exists())
        current = auth._load_auth_store()["credential_pool"][policy.PROVIDER]
        for row in current[:2]:
            self.assertIsNone(row.get("last_status"))
        self.assertEqual(current[2], self.other)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    checkout = parser.parse_args().checkout.resolve()
    sys.path.insert(0, str(checkout))
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory() as scratch, ExitStack() as offline:
        offline.enter_context(patch.dict(os.environ, {"HOME": scratch, "HERMES_HOME": scratch,
                                                    "HERMES_DISABLE_LAZY_INSTALLS": "1"}))
        for method in ("connect", "connect_ex"):
            offline.enter_context(patch.object(socket.socket, method, deny_network))
        from hermes_cli import auth, auth_codex as provider, psy_codex_policy as policy
        unittest.main(argv=[sys.argv[0]])
