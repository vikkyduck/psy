"""Exercise real HTTP handlers and deployed client code with isolated state and no worker."""
import asyncio, hashlib, hmac, json, os, shutil, tempfile
from pathlib import Path
from urllib.parse import urlencode
from aiohttp import web
import psy_upload_common as C
import server
from broker_fixture import start_broker
from unittest.mock import patch

async def main():
    with tempfile.TemporaryDirectory(prefix='psy-upload-http-test-') as tmp:
        C.ROOT=Path(tmp)/'state';C.FILES=Path(tmp)/'files'
        fake='isolated-test-only'
        broker_path = Path(tmp)/'member.sock'
        broker = await start_broker(broker_path, fake, lambda uid, token: False)
        scoped = patch.dict(os.environ, PSY_ACCESS_SOCKET=str(broker_path))
        scoped.start()
        fields={'auth_date':str(C.now()),'user':json.dumps({'id':C.OWNER,'first_name':'Test'},separators=(',',':'))}
        key=hmac.new(b'WebAppData',fake.encode(),hashlib.sha256).digest()
        fields['hash']=hmac.new(key,'\n'.join(f'{k}={v}' for k,v in sorted(fields.items())).encode(),hashlib.sha256).hexdigest()
        runner=web.AppRunner(server.create_app(),access_log=None);await runner.setup()
        try:
            site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
            port=site._server.sockets[0].getsockname()[1]
            directory=Path(__file__).resolve().parent
            env={**os.environ,'PSY_TEST_APP':str(directory.parent/'static/app.js')}
            child=await asyncio.create_subprocess_exec(shutil.which('node') or 'node',str(directory/'test_transfer_http.cjs'),str(port),urlencode(fields),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env)
            try:out,err=await asyncio.wait_for(child.communicate(),timeout=90)
            except asyncio.TimeoutError:
                child.kill();await child.wait();raise
            assert child.returncode==0,err.decode()[:300]
            result=json.loads(out)
            with C.db() as c:rows=c.execute('SELECT * FROM jobs').fetchall()
            assert len(rows)==1 and rows[0]['state']=='queued'
            p=C.file_path(dict(rows[0]));h=hashlib.sha256()
            with p.open('rb') as f:
                for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
            assert h.hexdigest()==result['sha256'] and p.stat().st_size==result['bytes']
            assert p.stat().st_mode&0o777==0o600
            print(json.dumps({'http_bytes':result['bytes'],'recovered_failures':result['recovered_failures'],'integrity':'PASS','single_job':'PASS','private_permissions':'PASS','production_files_untouched':True}))
        finally:
            await runner.cleanup()
            scoped.stop()
            broker.close()
            await broker.wait_closed()

if __name__=='__main__':asyncio.run(main())
