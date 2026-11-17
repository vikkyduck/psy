import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer
import psy_upload_common as C
import server
import recording_receipts as receipts
from broker_fixture import start_broker

TOKEN='test-credential-only'


def signed(uid,age=0,**extra):
    fields={'auth_date':str(C.now()-age),'user':json.dumps({'id':uid,'first_name':'Test'},separators=(',',':')),'query_id':'test'}
    fields.update(extra)
    key=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    fields['hash']=hmac.new(key,'\n'.join(f'{k}={v}' for k,v in sorted(fields.items())).encode(),hashlib.sha256).hexdigest()
    return urlencode(fields)


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.oldroot=C.ROOT;self.oldfiles=C.FILES;C.ROOT=Path(self.temp.name);C.FILES=C.ROOT/'files'
        path = C.ROOT/'access.sock'
        broker = await start_broker(path, TOKEN, lambda uid, token: uid == C.OWNER)
        self.addAsyncCleanup(broker.wait_closed)
        self.addCleanup(broker.close)
        scoped = patch.dict(os.environ, PSY_ACCESS_SOCKET=str(path))
        scoped.start()
        self.addCleanup(scoped.stop)
        self.oldmembership=server.membership
        async def member(request,uid):return uid in (C.OWNER,123)
        server.membership=member
        self.client=TestClient(TestServer(server.create_app()));await self.client.start_server()
        r=await self.client.post('/api/auth',json={'initData':signed(C.OWNER)})
        self.assertEqual(r.status,200)
        self.auth={'X-Upload-Session':(await r.json())['session']}

    async def asyncTearDown(self):
        await self.client.close();C.ROOT=self.oldroot;C.FILES=self.oldfiles;server.membership=self.oldmembership;self.temp.cleanup()

    def seal(self,ident,text):
        with C.db() as c:
            row=dict(c.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone())
        receipts.save(row,dict(duration=3,speech_detected=True),text,delivery=row['delivery'] or 'ready',message_id=row['message_id'])

    async def new_job(self,size,head,auth=None,name='recording.wav',**extra):
        manifest=hashlib.sha256()
        if size<=C.LIMIT:
            for off in range(0,size,C.HASH_BLOCK):
                length=min(C.HASH_BLOCK,size-off)
                block=(head*((length+len(head)-1)//len(head)))[:length]
                manifest.update(hashlib.sha256(block).digest())
        b={'filename':name,'size':size,'fingerprint':hashlib.sha256(head[:min(size,65536)]).hexdigest(),
           'content_id':manifest.hexdigest(),'language':'ru',**extra}
        return await self.client.post('/api/jobs',json=b,headers=auth or self.auth)

    async def test_invalid_auth_and_access_boundaries(self):
        for raw in (signed(C.OWNER)+'&user=bad',signed(C.OWNER,601),signed(C.OWNER).replace('Test','Changed')):
            r=await self.client.post('/api/auth',json={'initData':raw});self.assertEqual(r.status,401)
        r=await self.client.post('/api/auth',json={'initData':signed(123)});self.assertEqual(r.status,403)
        ident=C.ticket(123,C.GROUP)
        r=await self.client.post('/api/auth',json={'initData':signed(C.OWNER),'ticket':ident});self.assertEqual(r.status,403)
        r=await self.client.post('/api/auth',json={'initData':signed(123),'ticket':ident});self.assertEqual(r.status,200)
        other={'X-Upload-Session':(await r.json())['session']}
        r=await self.new_job(65536,b'x'*65536);job=(await r.json())['id']
        r=await self.client.get('/api/jobs',headers=other);self.assertEqual((await r.json())['jobs'],[])
        r=await self.client.patch('/api/jobs/'+job,data=b'x',headers={**other,'Upload-Offset':'0'});self.assertEqual(r.status,404)
        r=await self.client.get('/api/jobs');self.assertEqual(r.status,401)

    async def test_large_resume_integrity_and_private_destination(self):
        block=b'\x53'*C.CHUNK;size=6*C.CHUNK+123
        r=await self.new_job(size,block,chat=C.GROUP);self.assertEqual(r.status,201);ident=(await r.json())['id']
        for offset in (0,C.CHUNK):
            r=await self.client.patch('/api/jobs/'+ident,data=block,headers={**self.auth,'Upload-Offset':str(offset)});self.assertEqual(r.status,200)
        r=await self.client.patch('/api/jobs/'+ident,data=block,headers={**self.auth,'Upload-Offset':'0'});self.assertEqual(r.status,409)
        r=await self.client.post('/api/jobs/'+ident+'/complete',headers=self.auth);self.assertEqual(r.status,409)
        # Uncommitted bytes from a terminated upload must be truncated before resuming.
        with C.db() as c:j=dict(c.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone())
        self.assertEqual(j['chat'],C.OWNER)
        with C.file_path(j).open('ab') as f:f.write(b'uncommitted-tail')
        for offset in range(2*C.CHUNK,size,C.CHUNK):
            part=block[:min(C.CHUNK,size-offset)]
            r=await self.client.patch('/api/jobs/'+ident,data=part,headers={**self.auth,'Upload-Offset':str(offset)});self.assertEqual(r.status,200)
        self.assertEqual(C.file_path(j).stat().st_size,size)
        self.assertEqual(C.file_path(j).stat().st_mode&0o777,0o600)
        with C.file_path(j).open('rb') as f:
            while part:=f.read(C.CHUNK):self.assertEqual(part,b'\x53'*len(part))
        r=await self.client.post('/api/jobs/'+ident+'/complete',headers=self.auth);self.assertEqual(r.status,200)
        r=await self.client.post('/api/jobs/'+ident+'/complete',headers=self.auth);self.assertEqual(r.status,200)

    async def test_origin_type_quota_and_cancel(self):
        r=await self.client.post('/api/jobs',headers={**self.auth,'Origin':'https://untrusted.invalid'},json={});self.assertEqual(r.status,403)
        r=await self.new_job(C.LIMIT+1,b'x'*65536);self.assertEqual(r.status,400)
        r=await self.new_job(65536,b'x'*65536,name='script.py');self.assertEqual(r.status,400)
        ids=[]
        for i in range(3):
            r=await self.new_job(65536,b'x'*65536,name=f'{i}.wav');self.assertEqual(r.status,201);ids.append((await r.json())['id'])
        r=await self.new_job(65536,b'x'*65536);self.assertEqual(r.status,409)
        r=await self.client.post('/api/jobs/'+ids[0]+'/cancel',headers=self.auth);self.assertEqual(r.status,200)
        r=await self.new_job(65536,b'x'*65536);self.assertEqual(r.status,201)
        r=await self.client.get('/assets/../../state.sqlite3');self.assertEqual(r.status,404)

    async def test_wrong_content_rolls_back(self):
        r=await self.new_job(65536,b'x'*65536);ident=(await r.json())['id']
        r=await self.client.patch('/api/jobs/'+ident,data=b'z'*65536,headers={**self.auth,'Upload-Offset':'0'});self.assertEqual(r.status,400)
        with C.db() as c:j=dict(c.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone())
        self.assertEqual(j['offset'],0);self.assertEqual(C.file_path(j).stat().st_size,0)

    async def test_group_member_destination_and_other_member_isolation(self):
        async def member(request,uid):return uid in (123,124)
        server.membership=member
        group_auth=[]
        for uid in (123,124):
            ticket=C.ticket(uid,C.GROUP,thread=19,reply=70+uid)
            r=await self.client.post('/api/auth',json={'initData':signed(uid),'ticket':ticket})
            self.assertEqual(r.status,200)
            payload=await r.json()
            self.assertEqual(payload['destination'],'Группа')
            group_auth.append({'X-Upload-Session':payload['session']})
        r=await self.new_job(65536,b'x'*65536,auth=group_auth[0],chat=C.OWNER,uid=C.OWNER)
        self.assertEqual(r.status,201);ident=(await r.json())['id']
        with C.db() as c:j=dict(c.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone())
        self.assertEqual((j['uid'],j['chat'],j['thread'],j['reply']),(123,C.GROUP,19,193))
        r=await self.client.get('/api/jobs',headers=group_auth[1]);self.assertEqual((await r.json())['jobs'],[])
        r=await self.client.get('/api/jobs/'+ident+'/transcript',headers=group_auth[1]);self.assertEqual(r.status,404)
        r=await self.client.patch('/api/jobs/'+ident,data=b'x'*65536,headers={**group_auth[0],'Upload-Offset':'0'})
        self.assertEqual(r.status,200)
        r=await self.client.post('/api/jobs/'+ident+'/complete',headers=group_auth[0]);self.assertEqual(r.status,200)
        with C.db() as c:j=dict(c.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone())
        self.assertEqual((j['state'],j['chat']),('queued',C.GROUP))

    async def test_group_membership_revoked_before_finalization(self):
        ticket=C.ticket(123,C.GROUP)
        r=await self.client.post('/api/auth',json={'initData':signed(123),'ticket':ticket})
        self.assertEqual(r.status,200);auth={'X-Upload-Session':(await r.json())['session']}
        r=await self.new_job(65536,b'x'*65536,auth=auth);ident=(await r.json())['id']
        r=await self.client.patch('/api/jobs/'+ident,data=b'x'*65536,headers={**auth,'Upload-Offset':'0'})
        self.assertEqual(r.status,200)
        async def revoked(request,uid):return False
        server.membership=revoked
        r=await self.client.post('/api/jobs/'+ident+'/complete',headers=auth);self.assertEqual(r.status,403)
        r=await self.client.post('/api/auth',json={'initData':signed(123),'ticket':ticket});self.assertEqual(r.status,403)

    async def test_shared_group_entry_signed_destination_and_member_isolation(self):
        async def member(request,uid):return uid in (C.OWNER,123,124)
        server.membership=member
        sessions=[]
        for uid in (C.OWNER,123,124):
            r=await self.client.post('/api/auth',json={'initData':signed(uid,start_param='psy_group'),'chat':C.OWNER})
            self.assertEqual(r.status,200)
            b=await r.json();self.assertEqual(b['destination'],'Группа')
            sessions.append({'X-Upload-Session':b['session']})
        r=await self.new_job(65536,b'x'*65536,auth=sessions[1],chat=C.OWNER,uid=C.OWNER)
        self.assertEqual(r.status,201);ident=(await r.json())['id']
        with C.db() as c:j=dict(c.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone())
        self.assertEqual((j['uid'],j['chat'],j['thread'],j['reply']),(123,C.GROUP,None,None))
        r=await self.client.get('/api/jobs',headers=sessions[2]);self.assertEqual((await r.json())['jobs'],[])
        r=await self.client.get('/api/jobs/'+ident+'/transcript',headers=sessions[2]);self.assertEqual(r.status,404)
        r=await self.client.post('/api/auth',json={'initData':signed(999,start_param='psy_group')});self.assertEqual(r.status,403)
        async def revoked(request,uid):return False
        server.membership=revoked
        r=await self.client.post('/api/auth',json={'initData':signed(123,start_param='psy_group')});self.assertEqual(r.status,403)
        # Existing sessions recheck membership after the bounded 30-second cache.
        self.client.server.app['members'][123]=C.now()-1
        r=await self.client.get('/api/jobs',headers=sessions[1]);self.assertEqual(r.status,403)

    async def test_shared_group_entry_rejects_unsigned_tampered_and_expired_context(self):
        for body,status in (
            ({'initData':signed(123),'start_param':'psy_group'},403),
            ({'initData':signed(123)+'&start_param=psy_group'},401),
            ({'initData':signed(123,start_param='psy_group').replace('psy_group','another_group')},401),
            ({'initData':signed(123,601,start_param='psy_group')},401),
            ({'initData':signed(C.OWNER,start_param='unknown')},403),
            ({'initData':signed(123,start_param='psy_group'),'ticket':'invalid'},403),
        ):
            r=await self.client.post('/api/auth',json=body);self.assertEqual(r.status,status)
        # The existing owner's private menu must still return results privately.
        r=await self.client.post('/api/auth',json={'initData':signed(C.OWNER),'start_param':'psy_group'})
        self.assertEqual(r.status,200);self.assertEqual((await r.json())['destination'],'Личная переписка с PSY')

    async def test_progress_reads_checkpoint_without_exposing_private_text_or_paths(self):
        r=await self.new_job(65536,b'x'*65536);ident=(await r.json())['id']
        with C.db() as c:
            c.execute("UPDATE jobs SET state='processing' WHERE id=?",(ident,))
            j=dict(c.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone())
        with patch.dict(os.environ, PSY_ASR_PROGRESS=self.temp.name):
            progress_file = Path(self.temp.name)/(ident+'.json')
            progress_file.write_text(json.dumps({'total_seconds':100,'processed_seconds':60,
                                                  'private':'PRIVATE TRANSCRIPT TEXT'}))
            r=await self.client.get('/api/jobs',headers=self.auth)
            body=await r.json()
            self.assertEqual(body['jobs'][0]['progress'],{'processed_seconds':60.0,'total_seconds':100.0,'percent':60})
            self.assertNotIn('PRIVATE TRANSCRIPT',json.dumps(body))
            self.assertNotIn(self.temp.name,json.dumps(body))
            progress_file.write_text('{truncated')
            r=await self.client.get('/api/jobs',headers=self.auth)
            self.assertIsNone((await r.json())['jobs'][0]['progress'])


    async def test_same_file_creation_is_idempotent_and_full_digest_rejects_mixed_audio(self):
        import asyncio
        prefix=b'a'*65536
        expected=prefix+b'b'*1024
        responses=await asyncio.gather(*(self.new_job(len(expected),expected) for _ in range(2)))
        self.assertEqual(sorted(r.status for r in responses),[200,201])
        ids=[(await r.json())['id'] for r in responses]
        self.assertEqual(ids[0],ids[1])
        corrupt=prefix+b'c'*1024
        saved=await self.client.patch('/api/jobs/'+ids[0],data=corrupt,headers={**self.auth,'Upload-Offset':'0'})
        self.assertEqual(saved.status,200)
        result=await self.client.post('/api/jobs/'+ids[0]+'/complete',headers=self.auth)
        self.assertEqual(result.status,422)
        with C.db() as conn:
            self.assertEqual(conn.execute('SELECT state FROM jobs').fetchone()['state'],'cancelled')

    async def test_stale_draft_releases_reservation(self):
        response=await self.new_job(100,b'a'*100)
        old=(await response.json())['id']
        with C.db() as c:
            c.execute('UPDATE jobs SET updated=? WHERE id=?',(C.now()-C.DRAFT_TTL-10,old))
        response=await self.new_job(100,b'a'*100)
        self.assertEqual(response.status,201)
        self.assertNotEqual((await response.json())['id'],old)
        with C.db() as c:
            row=c.execute('SELECT * FROM jobs WHERE id=?',(old,)).fetchone()
            self.assertEqual(row['state'],'cancelled')
            self.assertFalse(C.file_path(row).exists())

    async def test_unknown_send_needs_explicit_retry_without_retranscription(self):
        result=await self.new_job(1,b'x')
        ident=(await result.json())['id']
        with C.db() as c:
            c.execute("UPDATE jobs SET state='done',delivery='unknown' WHERE id=?",(ident,))
        self.seal(ident,b'fixture')
        path='/api/jobs/'+ident+'/redeliver'
        self.assertEqual((await self.client.post(path,json={},headers=self.auth)).status,409)
        self.assertEqual((await self.client.post(path,json={'confirm_possible_duplicate':True},headers=self.auth)).status,200)
        with C.db() as c:
            self.assertEqual(c.execute('SELECT state FROM jobs WHERE id=?',(ident,)).fetchone()['state'],'delivery_pending')


    async def test_public_frontend_exchanges_only_scoped_requests_with_broker(self):
        import asyncio
        from unittest.mock import patch
        import access_broker
        calls=[]
        def availability(uid,token):
            if uid==125:raise TimeoutError()
            return uid==123
        async def handle(reader,writer):
            try:
                request=json.loads(await reader.readline())
                calls.append(request['action'])
                reply=access_broker.dispatch(request,TOKEN,availability)
                writer.write(json.dumps(reply).encode()+b'\n')
                await writer.drain()
            finally:writer.close()
        path=str(C.ROOT/'member.sock')
        sock=await asyncio.start_unix_server(handle,path=path)
        self.addAsyncCleanup(sock.wait_closed)
        self.addCleanup(sock.close)
        server.membership=self.oldmembership
        with patch.dict(os.environ,{'PSY_ACCESS_SOCKET':path}),patch.object(C,'bot_settings',side_effect=AssertionError('web attempted direct credential read')):
            for uid,status in ((123,200),(124,403),(125,503)):
                response=await self.client.post('/api/auth',json={'initData':signed(uid,start_param='psy_group')})
                self.assertEqual(response.status,status)
            forged=await self.client.post('/api/auth',json={'initData':signed(123)+'x'})
            self.assertEqual(forged.status,401)
            tampered=await self.client.post('/api/auth',json={'initData':signed(123).replace('Test','Other')})
            self.assertEqual(tampered.status,401)
        # A 65-character hash is rejected locally; a well-formed forgery still needs the broker.
        self.assertEqual(calls,['login','member','login','member','login','member','login'])

    async def test_malformed_launch_data_never_starts_a_broker(self):
        connections=[]
        async def count(reader,writer):
            connections.append(1)
            writer.close()
        path=str(C.ROOT/'counting.sock')
        sock=await asyncio.start_unix_server(count,path=path)
        self.addAsyncCleanup(sock.wait_closed)
        self.addCleanup(sock.close)
        valid=dict(item.split('=',1) for item in signed(C.OWNER).split('&'))
        def variant(**changes):
            fields={**valid,**changes}
            return '&'.join(f'{k}={v}' for k,v in fields.items() if v is not None)
        junk=['', 'x'*16385, 'not a query', 'hash=', variant(hash=None), variant(hash=valid['hash'].upper()),
              variant(hash=valid['hash'][:63]), variant(hash='g'*64), variant(auth_date='soon'),
              variant(auth_date=str(C.now()-601)), variant(auth_date=str(C.now()+61)), variant(user=None),
              variant(user='%7B'), variant(user='%5B%5D'), variant(user='%7B%22id%22%3A%227%22%7D'),
              variant(user='%7B%22id%22%3A0%7D'), variant(user='%7B%22id%22%3A7%2C%22is_bot%22%3Atrue%7D'),
              variant(user='%5B'*5000), signed(C.OWNER)+'&auth_date=1', 12345, None, ['list']]
        with patch.dict(os.environ,{'PSY_ACCESS_SOCKET':path}):
            for raw in junk:
                response=await self.client.post('/api/auth',json={'initData':raw,'ticket':'0'*32})
                self.assertEqual(response.status,401,raw if isinstance(raw,str) and len(raw)<200 else type(raw))
            self.assertEqual(connections,[])
            # Plausible launch data still reaches the broker, which alone checks the signature.
            response=await self.client.post('/api/auth',json={'initData':variant(hash='0'*64)})
            self.assertEqual(response.status,503)
            self.assertEqual(len(connections),1)

    def test_local_launch_check_is_never_stricter_than_the_broker(self):
        import access_broker
        now=C.now()
        for extra in ({'auth_date':str(now-600)},{'auth_date':str(now+30)},{'auth_date':' '+str(now)},
                      {'auth_date':'+'+str(now)},{'start_param':''},{'user':json.dumps({'id':5,'is_bot':False})}):
            fields={'auth_date':str(now),'user':json.dumps({'id':C.OWNER}),**extra}
            key=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
            fields['hash']=hmac.new(key,'\n'.join(f'{k}={v}' for k,v in sorted(fields.items())).encode(),hashlib.sha256).hexdigest()
            raw=urlencode(fields)
            self.assertEqual(access_broker.dispatch({'action':'init','init_data':raw},TOKEN)['status'],200,extra)
            # The web server checks first and up to the broker timeout earlier.
            self.assertTrue(C.plausible_init(raw,now),extra)
            self.assertTrue(C.plausible_init(raw,now-20),extra)


    async def test_transcript_streams_in_bounded_pieces(self):
        ident=(await (await self.new_job(1,b'x')).json())['id']
        with C.db() as c:
            c.execute("UPDATE jobs SET state='done',delivery='sent' WHERE id=?",(ident,))
        body=''.join(f'[00:00:{i%60:02d}] строка {i}\n' for i in range(40000)).encode()
        self.seal(ident,body)
        with patch.object(server,'TRANSCRIPT_PIECE',8192),patch.object(receipts,'read_body',wraps=receipts.read_body) as reads:
            response=await self.client.get('/api/jobs/'+ident+'/transcript',headers=self.auth)
            self.assertEqual(response.status,200)
            self.assertEqual(await response.read(),body)
        self.assertEqual(reads.call_count,-(-len(body)//8192))
        self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(int(response.headers['Content-Length']),len(body))
        # A transcript removed during a download ends the connection instead of a short success.
        def removed(*args):raise LookupError('gone')
        with patch.object(receipts,'read_body',removed):
            response=await self.client.get('/api/jobs/'+ident+'/transcript',headers=self.auth)
            with self.assertRaises(Exception):
                await response.read()
        self.assertEqual((await self.client.get('/api/jobs',headers=self.auth)).status,200)

    async def test_slow_disk_flush_does_not_stall_other_requests(self):
        import time
        ident=(await (await self.new_job(65536,b'x'*65536)).json())['id']
        real=os.fsync
        def slow(fd):
            time.sleep(0.8);real(fd)
        with patch('os.fsync',slow):
            upload=asyncio.ensure_future(self.client.patch('/api/jobs/'+ident,data=b'x'*65536,
                                                           headers={**self.auth,'Upload-Offset':'0'}))
            await asyncio.sleep(0.25)
            started=time.monotonic()
            listing=await self.client.get('/api/jobs',headers=self.auth)
            self.assertEqual(listing.status,200)
            self.assertLess(time.monotonic()-started,0.5)
            self.assertFalse(upload.done())
            self.assertEqual((await upload).status,200)

    async def test_concurrent_retries_share_create_limit_preserve_results_and_queue_fairly(self):
        import worker
        done=(await (await self.new_job(1,b'z',name='done.wav')).json())['id']
        saved=C.file_path({'id':done},'.txt')
        saved.write_bytes(b'preserve finished transcript')
        with C.db() as conn:
            conn.execute("UPDATE jobs SET state='done',delivery='sent',message_id=42,updated=123 WHERE id=?",(done,))
        already=(await (await self.new_job(1,b'w',name='other.wav')).json())['id']
        with C.db() as conn:
            conn.execute("UPDATE jobs SET uid=123,state='queued' WHERE id=?",(already,))
        failed=[]
        for i in range(4):
            ident=(await (await self.new_job(2,b'xx',name=f'{i}.wav')).json())['id']
            failed.append(ident)
            with C.db() as conn:
                conn.execute("UPDATE jobs SET state='failed',created=1,attempts=7,next_try=?,error='test' WHERE id=?",
                             (C.now()+14400,ident))
        async def retry(ident,generation=0):
            return await self.client.post('/api/jobs/'+ident+'/retry',headers=self.auth,json={'retry_generation':generation})
        responses=await asyncio.gather(*(retry(ident) for ident in failed))
        self.assertEqual(sorted(r.status for r in responses),[200,200,200,409])
        accepted=next(ident for ident,r in zip(failed,responses) if r.status==200)
        self.assertEqual((await retry(accepted)).status,200)
        self.assertEqual((await self.new_job(1,b'n',name='new.wav')).status,409)
        with C.db() as conn:
            active=conn.execute("SELECT * FROM jobs WHERE uid=? AND state='queued'",(C.OWNER,)).fetchall()
            self.assertEqual(len(active),3)
            self.assertTrue(all((j['retry_generation'],j['attempts'],j['next_try'],j['error'])==(1,0,0,None) for j in active))
            self.assertEqual(worker.next_ready_job(conn)['id'],already)
            conn.execute("UPDATE jobs SET state='failed' WHERE id=?",(accepted,))
        replay=await retry(accepted)
        self.assertEqual((await replay.json())['state'],'failed')
        self.assertEqual((await retry(accepted,1)).status,200)
        self.assertEqual((await (await retry(done)).json())['state'],'done')
        with C.db() as conn:
            receipt=conn.execute('SELECT delivery,message_id,updated FROM jobs WHERE id=?',(done,)).fetchone()
        self.assertEqual(tuple(receipt),('sent',42,123))
        self.assertEqual(saved.read_bytes(),b'preserve finished transcript')

    async def test_explicit_repeat_versions_preserve_audio_results_and_scope(self):
        # Exercise two web sessions submitting the repeat and a lost-response replay.
        group = C.ticket(123,C.GROUP,thread=23,reply=75)
        response = await self.client.post('/api/auth',json={'initData':signed(123),'ticket':group})
        auth = {'X-Upload-Session': (await response.json())['session']}
        audio = b'synthetic audio'
        source_id = (await (await self.new_job(len(audio),audio,auth=auth)).json())['id']
        self.assertEqual((await self.client.patch('/api/jobs/'+source_id,data=audio,
                          headers={**auth,'Upload-Offset':'0'})).status,200)
        self.assertEqual((await self.client.post('/api/jobs/'+source_id+'/complete',headers=auth)).status,200)
        route = '/api/jobs/'+source_id+'/reprocess'
        self.assertEqual((await self.client.post(route,headers=auth)).status,409)
        with C.db() as conn:
            conn.execute("UPDATE jobs SET state='done',delivery='sent',message_id=392 WHERE id=?",(source_id,))
        original_txt = C.file_path({'id':source_id},'.txt')
        original_txt.write_bytes(b'first transcript')
        self.seal(source_id,b'first transcript')
        self.assertEqual((await self.client.post(route,headers=self.auth)).status,404)
        with patch.object(server, 'membership', return_value=False):
            self.assertEqual((await self.client.post(route,headers=auth)).status,403)
        responses = await asyncio.gather(*(self.client.post(route,headers=auth) for _ in range(2)))
        self.assertEqual(sorted(r.status for r in responses),[200,201])
        revisions = [(await r.json())['id'] for r in responses]
        self.assertEqual(revisions[0],revisions[1])
        repeat = revisions[0]
        self.assertNotEqual(repeat,source_id)
        with C.db() as conn:
            job = conn.execute('SELECT * FROM jobs WHERE id=?',(repeat,)).fetchone()
            original = conn.execute('SELECT * FROM jobs WHERE id=?',(source_id,)).fetchone()
            self.assertEqual(tuple(job[k] for k in ('uid','chat','thread','reply','state')),
                             (123,C.GROUP,23,75,'queued'))
            self.assertEqual(tuple(original[k] for k in ('state','delivery','message_id')),('done','sent',392))
            self.assertTrue(os.path.samefile(C.file_path(job), C.file_path(original)))
            self.assertFalse(C.file_path(job,'.txt').exists())
        self.assertEqual(original_txt.read_bytes(),b'first transcript')
        listing = (await (await self.client.get('/api/jobs',headers=auth)).json())['jobs']
        self.assertTrue(next(j for j in listing if j['id']==source_id)['has_next'])
        self.assertTrue(next(j for j in listing if j['id']==repeat)['is_repeat'])
        self.assertEqual((await self.client.get('/api/jobs/'+repeat+'/transcript',headers=self.auth)).status,404)
        self.assertEqual((await self.client.post(route,headers=auth)).status,200)

    async def test_member_capacity_admits_once_and_preserves_accepted_work(self):
        from storage_capacity import MAX_TRANSCRIPT_BYTES
        quota = MAX_TRANSCRIPT_BYTES + 300
        self.assertEqual((await self.client.get('/api/capacity')).status,401)
        with patch.dict(os.environ, PSY_MEMBER_QUOTA_BYTES=str(quota)):
            first,second = await asyncio.gather(*(self.new_job(200,b'a'*200,name=f'{i}.wav') for i in range(2)))
            self.assertEqual(sorted([first.status,second.status]),[201,409])
            accepted = first if first.status == 201 else second
            ident = (await accepted.json())['id']
            response = await self.client.get('/api/capacity', headers=self.auth)
            info = await response.json()
            self.assertEqual(set(info),{'member_limit_bytes','member_low','member_upload_bytes','disk_low'})
            self.assertEqual((info['member_limit_bytes'],info['member_upload_bytes'],info['member_low']),(quota,0,True))
            group = await self.client.post('/api/auth',json={'initData':signed(123,start_param='psy_group')})
            other = {'X-Upload-Session':(await group.json())['session']}
            capacity = await (await self.client.get('/api/capacity',headers=other)).json()
            self.assertEqual(capacity['member_upload_bytes'],300)
            with C.db() as conn:
                conn.execute("UPDATE jobs SET state='failed' WHERE id=?",(ident,))
            with patch.dict(os.environ, PSY_MEMBER_QUOTA_BYTES='1'):
                response = await self.client.post('/api/jobs/'+ident+'/retry',headers=self.auth,
                                                  json={'retry_generation':0})
                self.assertEqual(response.status,200)
                with C.db() as conn:
                    conn.execute("UPDATE jobs SET state='done' WHERE id=?",(ident,))
                self.seal(ident,b'fixture')
                response = await self.client.post('/api/jobs/'+ident+'/reprocess',headers=self.auth)
                self.assertEqual(response.status,409)
                self.assertIn('лимита', (await response.json())['error'])

    async def test_disk_capacity_warns_before_rejecting_and_checks_space_on_retry(self):
        from types import SimpleNamespace
        from storage_capacity import MAX_TRANSCRIPT_BYTES
        with patch('shutil.disk_usage',return_value=SimpleNamespace(free=C.RESERVE+MAX_TRANSCRIPT_BYTES+16)):
            res = await self.client.get('/api/capacity', headers=self.auth)
            self.assertTrue((await res.json())['disk_low'])
            self.assertEqual((await self.new_job(17,b'x'*17)).status,507)
            res = await self.new_job(16,b'x'*16)
            self.assertEqual(res.status,201)
            ident = (await res.json())['id']
        with C.db() as conn:
            conn.execute("UPDATE jobs SET state='failed' WHERE id=?",(ident,))
        with patch('shutil.disk_usage',return_value=SimpleNamespace(free=C.RESERVE+MAX_TRANSCRIPT_BYTES-1)):
            response = await self.client.post('/api/jobs/'+ident+'/retry',headers=self.auth,json={'retry_generation':0})
            self.assertEqual(response.status,507)
        with C.db() as conn:
            self.assertEqual(conn.execute('SELECT state FROM jobs WHERE id=?',(ident,)).fetchone()['state'],'failed')


if __name__=='__main__':unittest.main()
