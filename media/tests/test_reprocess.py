"""Exercise repeat requests and delivery using synthetic accounts, recordings and loopback HTTP."""
import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

import asr_client
import psy_upload_common as C
import server
import worker
import recording_receipts as receipts
from test_upload import TOKEN, signed
from broker_fixture import start_broker


class ReprocessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.member_ids = {171, 172}
        self.deliveries = []
        self.recognized = []
        telegram = web.Application()
        base = "/bot" + TOKEN + "/"
        telegram.add_routes([
            web.post(base + "getChatMember", self.member),
            web.post(base + "sendDocument", self.receive),
        ])
        upstream = TestServer(telegram)
        await upstream.start_server()
        self.addAsyncCleanup(upstream.close)
        broker_path = root / 'member.sock'
        broker = await start_broker(broker_path, TOKEN, lambda uid, token: uid in self.member_ids)
        self.addAsyncCleanup(broker.wait_closed)
        self.addCleanup(broker.close)
        for scoped in (
            patch.multiple(C, ROOT=root / "state", FILES=root / "files", HOME=root, OWNER=171, GROUP=-707),
            patch.object(C, "bot_settings", return_value=(TOKEN, str(upstream.make_url("/bot")))),
            patch.dict(os.environ, PSY_ACCESS_SOCKET=str(broker_path), PSY_ASR_PROGRESS=""),
            patch.object(asr_client, "transcribe_recording", self.recognize),
        ):
            scoped.start()
            self.addCleanup(scoped.stop)
        self.client = TestClient(TestServer(server.create_app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.group = await self.login(C.OWNER, C.GROUP, thread=12, reply=51)
        self.private = await self.login(C.OWNER, C.OWNER)
        self.partner = await self.login(172, C.GROUP)
        self.audio = b"Synthetic reprocess fixture"
        content = hashlib.sha256(self.audio)
        self.info = dict(filename="sample.wav", size=len(self.audio), language="ru",
                         fingerprint=content.hexdigest(), content_id=hashlib.sha256(content.digest()).hexdigest())

    async def member(self, request):
        payload = await (request.json() if request.content_type == "application/json" else request.post())
        self.assertEqual(int(payload["chat_id"]), C.GROUP)
        status = "member" if int(payload["user_id"]) in self.member_ids else "left"
        return web.json_response({"ok": True, "result": {"status": status}})

    async def receive(self, request):
        payload = await request.post()
        reply = json.loads(payload["reply_parameters"]) if "reply_parameters" in payload else {}
        self.deliveries.append((int(payload["chat_id"]), payload.get("message_thread_id"),
                                reply.get("message_id"), payload["document"].file.read()))
        return web.json_response({"ok": True, "result": {"message_id": 100 + len(self.deliveries)}})

    async def recognize(self, job, destination, saved=None):
        self.recognized.append(job["id"])
        destination.write_bytes(("fixture transcript " + job["id"]).encode())
        header = {"complete": True, "duration": 1.25, "speech_detected": True, "sha256":hashlib.sha256(self.audio).hexdigest()}
        if saved is not None:
            await saved(header)
        return header

    async def login(self, uid, chat, thread=None, reply=None):
        body = {"initData": signed(uid)}
        if chat == C.GROUP:
            body["ticket"] = C.ticket(uid, chat, thread, reply)
        response = await self.client.post("/api/auth", json=body)
        self.assertEqual(response.status, 200)
        return {"X-Upload-Session": (await response.json())["session"]}

    def job(self, ident):
        with C.db() as db:
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone())

    async def upload(self, headers):
        response = await self.client.post("/api/jobs", json=self.info, headers=headers)
        self.assertEqual(response.status, 201)
        ident = (await response.json())["id"]
        path = "/api/jobs/" + ident
        response = await self.client.patch(path, data=self.audio, headers={**headers, "Upload-Offset": "0"})
        self.assertEqual(response.status, 200)
        response = await self.client.post(path + "/complete", headers=headers)
        self.assertEqual(response.status, 200)
        return ident

    async def finish(self, ident):
        async with ClientSession() as http:
            await worker.run_job(http, self.job(ident))
            self.assertEqual(self.job(ident)["state"], "delivery_pending")
            await worker.run_job(http, self.job(ident))
            self.assertEqual(self.job(ident)["delivery"], "sent")

    async def test_concurrent_and_replayed_request_preserves_versions_and_delivery_route(self):
        for headers, chat, thread, reply in ((self.group, C.GROUP, "12", 51), (self.private, C.OWNER, None, None)):
            with self.subTest(chat=chat):
                parent = await self.upload(headers)
                path = "/api/jobs/" + parent
                premature = await self.client.post(path + "/reprocess", headers=headers)
                self.assertEqual(premature.status, 409)
                await self.finish(parent)
                original_row = self.job(parent)
                original = await (await self.client.get(path + "/transcript", headers=headers)).read()
                self.assertEqual(self.deliveries[-1], (chat, thread, reply, original))
                ordinary = await self.client.post("/api/jobs", json=self.info, headers=headers)
                self.assertEqual((ordinary.status, (await ordinary.json())["id"]), (200, parent))

                # A session from a different thread may read its own cabinet. Delivery must
                # follow the recording's thread and reply, even with forged body parameters.
                current = await self.login(C.OWNER, C.GROUP, thread=91, reply=82) if thread else headers
                forged = {"chat": 999, "thread": 888, "reply": 777, "uid": 666}
                responses = await asyncio.gather(*(self.client.post(path + "/reprocess", headers=current, json=forged)
                                                   for _ in range(4)))
                self.assertEqual(sorted(response.status for response in responses), [200, 200, 200, 201])
                ids = [(await response.json())["id"] for response in responses]
                child = ids[0]
                self.assertEqual(len(set(ids)), 1)
                self.assertNotEqual(child, parent)
                self.assertTrue(C.file_path(original_row).samefile(C.file_path(self.job(child))))
                self.assertFalse(C.file_path(self.job(child), ".txt").exists())
                await self.finish(child)
                successor_text = await self.client.get("/api/jobs/" + child + "/transcript", headers=headers)
                self.assertEqual(successor_text.status, 200)
                new = await successor_text.read()
                self.assertEqual(self.deliveries[-1], (chat, thread, reply, new))
                self.assertNotEqual(original, new)
                replay = await self.client.post(path + "/reprocess", headers=current)
                self.assertEqual((replay.status, (await replay.json())["id"]), (200, child))
                self.assertEqual(self.job(parent), original_row)
                self.assertEqual(await (await self.client.get(path + "/transcript", headers=headers)).read(), original)
                self.assertEqual(self.recognized.count(child), 1)
                listing = (await (await self.client.get("/api/jobs", headers=headers)).json())["jobs"]
                flags = {job["id"]: (job["has_next"], job["is_repeat"]) for job in listing}
                self.assertEqual(flags, {parent: (True, False), child: (False, True)})
        self.assertEqual(len(self.deliveries), 4)

    async def test_other_cabinets_and_revoked_membership_cannot_repeat_or_read(self):
        parent = await self.upload(self.group)
        await self.finish(parent)
        path = "/api/jobs/" + parent
        for headers, status in (({}, 401), (self.private, 404), (self.partner, 404)):
            self.assertEqual((await self.client.post(path + "/reprocess", headers=headers)).status, status)
            self.assertEqual((await self.client.get(path + "/transcript", headers=headers)).status, status)
        repeat = await self.client.post(path + "/reprocess", headers=self.group)
        self.assertEqual(repeat.status, 201)
        child = (await repeat.json())["id"]
        child_path = "/api/jobs/" + child
        await self.finish(child)
        for headers in (self.private, self.partner):
            self.assertEqual((await self.client.get(child_path + "/transcript", headers=headers)).status, 404)
            self.assertEqual((await self.client.post(child_path + "/reprocess", headers=headers)).status, 404)
            self.assertEqual((await (await self.client.get("/api/jobs", headers=headers)).json())["jobs"], [])
        # The owner has left the group and keeps the group destination.
        self.member_ids.remove(C.OWNER)
        self.assertEqual((await self.client.post(path + "/reprocess", headers=self.group)).status, 200)
        self.assertEqual((await self.client.post(child_path + "/reprocess", headers=self.group)).status, 201)
        self.assertEqual(len(self.deliveries), 2)
        partner = await self.upload(self.partner)
        await self.finish(partner)
        self.assertEqual(len(self.deliveries), 3)
        self.member_ids.remove(172)
        self.assertEqual((await self.client.post("/api/jobs/" + partner + "/reprocess", headers=self.partner)).status, 403)

    async def test_legacy_recording_repeat_without_member_quota(self):
        parent = await self.upload(self.private)
        await self.finish(parent)
        path = "/api/jobs/" + parent
        # Completed recordings from older installations may lack content_id and sha256.
        with C.db() as db:
            db.execute("UPDATE jobs SET content_id=NULL,sha256=NULL WHERE id=?", (parent,))
        row = self.job(parent)
        text = receipts.get(row["id"],body=True)["body"]
        accepted = await self.client.post(path + "/reprocess", headers=self.private)
        self.assertEqual(accepted.status, 201)
        child = (await accepted.json())["id"]
        await self.finish(child)
        self.assertEqual(self.job(child)["sha256"], hashlib.sha256(self.audio).hexdigest())
        self.assertEqual(self.job(parent), row)
        self.assertEqual(receipts.get(row["id"],body=True)["body"], text)
        self.assertEqual(self.deliveries[-1][0], C.OWNER)

    async def test_source_unavailable_keeps_original_download_and_receipt(self):
        parent = await self.upload(self.private)
        await self.finish(parent)
        row = self.job(parent)
        path = "/api/jobs/" + parent
        text = receipts.get(row["id"],body=True)["body"]
        C.file_path(row).unlink()
        self.assertEqual((await self.client.post(path + "/reprocess", headers=self.private)).status, 409)
        self.assertEqual(await (await self.client.get(path + "/transcript", headers=self.private)).read(), text)
        self.assertEqual(self.job(parent), row)
        with C.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
