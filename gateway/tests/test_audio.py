import asyncio
import importlib
from pathlib import Path
import hashlib
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace as S
from unittest.mock import AsyncMock, patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"media"))
import upload_broker as broker
from psy_modules import psy_group_audio as audio


class Tests(unittest.IsolatedAsyncioTestCase):
    def test_broker_scope_ttl_and_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/"s.sqlite")
            with sqlite3.connect(path) as c:
                c.execute("CREATE TABLE sessions(hash PRIMARY KEY, uid,chat,thread,reply,expires,proof)")
            req={"uid":11, "thread":3, "reply":9}
            with patch.object(broker.recording_grant,"key",return_value=b"x"*32):
                token=broker.issue(req,path)
            with sqlite3.connect(path) as c:
                data=c.execute("SELECT * FROM sessions").fetchone()
            self.assertEqual(data[:5],(hashlib.sha256(token.encode()).hexdigest(),11,audio.GROUP,3,9))
            self.assertLessEqual(abs(time.time()+300-data[5]),2)
            for invalid in ({**req,"chat":1},{**req,"uid":True},{**req,"thread":-7},{**req,"reply":"path"}):
                with self.assertRaises(ValueError): broker.issue(invalid,path)
    def test_broker_issues_every_session_a_member_asks_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/"s.sqlite")
            with sqlite3.connect(path) as c:
                c.execute("CREATE TABLE sessions(hash PRIMARY KEY, uid,chat,thread,reply,expires,proof)")
            with patch.object(broker.recording_grant,"key",return_value=b"x"*32):
                tokens={broker.issue({"uid":11,"thread":None,"reply":n},path) for n in range(1,41)}
            self.assertEqual(len(tokens),40)
            with sqlite3.connect(path) as c:
                self.assertEqual(c.execute("SELECT count(*) FROM sessions WHERE uid=11").fetchone()[0],40)
    async def test_upload_resume_content_checked_and_final(self):
        data=b"abc"*590000
        accepted=[]
        class Response:
            status_code=200
            def __init__(self,d):self.d=d
            def json(self):return self.d
        async def req(method,path,**kw):
            if path.endswith("complete"):return Response({"state":"queued"})
            if method=="POST":
                self.assertEqual(kw["json"]["fingerprint"],hashlib.sha256(data[:65536]).hexdigest())
                return Response({"id":"a"*32,"offset":0})
            accepted.append(kw["content"])
            return Response({"offset":sum(map(len,accepted))})
        await audio.send_data(S(request=req),"voice.ogg",data)
        self.assertEqual(b"".join(accepted),data)
    def test_only_current_group_files(self):
        m=S(chat=S(id=51),from_user=S(id=51,is_bot=False),sender_chat=None,voice=S(file_size=1024))
        self.assertIsNone(audio.metadata(m))
        m.chat.id=audio.GROUP
        self.assertEqual(audio.metadata(m)[1],"Голосовое.ogg")
        m.sender_chat=S()
        self.assertIsNone(audio.metadata(m))
    def test_long_recording_name_keeps_its_extension(self):
        m=S(chat=S(id=audio.GROUP),from_user=S(id=51,is_bot=False),sender_chat=None,
            document=S(file_name="лекция "*40+".M4A",file_size=1024))
        name=audio.metadata(m)[1]
        self.assertEqual((len(name),name[-4:]),(140,".M4A"))
        m.document.file_name="notes "*40+".txt"
        self.assertIsNone(audio.metadata(m))
if __name__=="__main__":unittest.main()
