"""Authenticated streaming uploads. No polling consumer and no public file directory."""
import asyncio
import concurrent.futures
import functools
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import ssl
import stat
import signal
import time
import weakref
from aiohttp import web
import psy_upload_common as C
import recording_receipts as receipts
from storage_capacity import MAX_TRANSCRIPT_BYTES, admission_reason, capacity_snapshot
from queue_health import PROGRESS_WARNING_SECONDS, processing_age

ASSETS = Path(__file__).parent / 'static'
TLS = Path('/etc/psy-upload/tls/current')
locks = weakref.WeakValueDictionary()
# SQLite, fsync and progress files block. They run here; each thread reuses one connection.
STORAGE = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='psy-storage')
TRANSCRIPT_PIECE = 64 * 1024
SECURITY_HEADERS = {
    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
    'Content-Security-Policy': "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org",
}


async def blocking(fn, *args, **kwargs):
    return await asyncio.get_running_loop().run_in_executor(STORAGE, functools.partial(fn, *args, **kwargs))


def failure(status, message):
    return web.json_response({'error': message}, status=status)


@web.middleware
async def boundary(request, handler):
    try:
        if request.method not in ('GET', 'HEAD') and request.headers.get('Origin') not in (None, C.PUBLIC_URL):
            return failure(403, 'Недопустимый источник запроса.')
        response = await handler(request)
    except web.HTTPException as e:
        response = failure(e.status, 'Запрос отклонен.')
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        response = failure(400, 'Проверьте данные запроса.')
    except Exception as e:
        logging.error('Upload request failed: %s', type(e).__name__)
        response = failure(503, 'Сервис временно занят. Попробуйте еще раз.')
    if not response.prepared:
        response.headers.update(SECURITY_HEADERS)
    return response


async def membership(request, uid):
    reply = await access_lookup({'action':'member','uid':uid})
    if type(reply.get('member')) is not bool:
        raise web.HTTPServiceUnavailable()
    return reply['member']


async def access_lookup(payload):
    async def exchange():
        reader,writer=await asyncio.open_unix_connection(os.environ['PSY_ACCESS_SOCKET'],limit=16384)
        try:
            writer.write(json.dumps(payload,separators=(',',':')).encode()+b'\n')
            await writer.drain()
            return json.loads(await reader.readline())
        finally:
            writer.close()
            await writer.wait_closed()
    try:
        reply=await asyncio.wait_for(exchange(),timeout=12)
        if not isinstance(reply,dict):
            raise ValueError('invalid response')
    except (KeyError,OSError,ValueError,asyncio.TimeoutError):
        raise web.HTTPServiceUnavailable() from None
    if reply.get('status')==401:raise web.HTTPUnauthorized()
    if reply.get('status')==403:raise web.HTTPForbidden()
    if reply.get('status')!=200:raise web.HTTPServiceUnavailable()
    return reply


async def authorize(request):
    s = await blocking(C.get_session, request.headers.get('X-Upload-Session', ''))
    if not s:
        raise web.HTTPUnauthorized()
    if s['chat']==C.GROUP:
        cached=request.app['members'].get(s['uid'])
        if not cached or cached<C.now():
            if not await membership(request,s['uid']):raise web.HTTPForbidden()
            request.app['members'][s['uid']]=C.now()+30
    return s


async def auth(request):
    if request.content_length is None or request.content_length > 20000:
        raise web.HTTPRequestEntityTooLarge(max_size=20000, actual_size=request.content_length or 0)
    body = await request.json()
    raw = body.get('initData')
    # Each broker lookup starts a process. Junk that cannot carry a valid signature stops here.
    if not C.plausible_init(raw):
        raise web.HTTPUnauthorized()
    ticket = None
    if body.get('ticket'):
        launch = await access_lookup({'action':'init','init_data':raw})
        ticket = await blocking(C.get_ticket, body['ticket'], launch['uid'])
        if not ticket:
            raise web.HTTPForbidden()
    reply = await access_lookup({'action':'login','init_data':raw,'ticket':ticket})
    session = reply['session']
    if session['chat'] == C.GROUP and not await membership(request, session['uid']):
        raise web.HTTPForbidden()
    await blocking(C.store_session, session)
    return web.json_response({'session':reply['secret'],'destination':'Группа' if session['chat']==C.GROUP else 'Личная переписка с PSY','limit':C.LIMIT,'chunk':C.CHUNK})


async def bound_request(request, session, action, **data):
    return await access_lookup({'action':action,'secret':request.headers['X-Upload-Session'],'session':session,**data})


def authorized_result(job, session, body=False):
    result = receipts.get(job['id'], body=body)
    if not result:
        raise web.HTTPNotFound()
    origin = json.loads(result['binding'])
    if any(origin[k] != session[k] for k in ('uid','chat')):
        raise web.HTTPNotFound()
    return origin,result



def owned_job(ident, session):
    with C.db() as c:
        row = c.execute('SELECT * FROM jobs WHERE id=? AND uid=? AND chat=?', (ident, session['uid'], session['chat'])).fetchone()
    if not row:
        raise web.HTTPNotFound()
    return dict(row)


def transcription_progress(j):
    if j['state'] in ('done','sending','delivery_pending') and j['duration']:
        return {'processed_seconds':j['duration'],'total_seconds':j['duration'],'percent':100}
    if j['state']!='processing':return None
    try:
        with (Path(os.environ['PSY_ASR_PROGRESS'])/(j['id']+'.json')).open('rb') as stream:
            details=json.loads(stream.read(4096))
        total,processed=float(details['total_seconds']),float(details['processed_seconds'])
        if not (math.isfinite(total) and 0<=processed<=total and total>0):return None
        return {'processed_seconds':processed,'total_seconds':total,'percent':round(100*processed/total)}
    except (OSError,KeyError,ValueError,TypeError):return None


def public_job(j):
    public={k: j[k] for k in ('id','filename','size','offset','fingerprint','language','state','created','updated','error','duration','delivery','retry_generation')}
    public['progress']=transcription_progress(j)
    public['progress_warning'] = (j['state'] == 'processing' and bool(os.getenv('PSY_ASR_PROGRESS'))
                                  and processing_age(j['updated'], j['id'], os.environ['PSY_ASR_PROGRESS'])
                                  >= PROGRESS_WARNING_SECONDS)
    return public


def job_listing(session):
    with C.db() as c:
        rows = c.execute("""SELECT j.*, EXISTS(SELECT 1 FROM jobs c WHERE c.reprocess_of=j.id) AS has_next
                            FROM jobs j WHERE uid=? AND chat=? ORDER BY created DESC,j.rowid DESC LIMIT 20""",
                         (session['uid'],session['chat'])).fetchall()
    return [dict(public_job(r), has_next=bool(r['has_next']), is_repeat=r['reprocess_of'] is not None) for r in rows]


async def jobs(request):
    s = await authorize(request)
    return web.json_response({'jobs': await blocking(job_listing, s)})


def busy_uploads():
    """Snapshot on the event loop; an in-flight PATCH/complete owns its lock even while its body stalls."""
    return frozenset(ident for ident, lock in list(locks.items()) if lock.locked())


def expire_drafts(conn, busy):
    stale=conn.execute("SELECT id,ext FROM jobs WHERE state='uploading' AND updated<?",
                       (C.now()-C.DRAFT_TTL,)).fetchall()
    for draft in stale:
        if draft['id'] in busy:continue
        C.file_path(draft).unlink(missing_ok=True)
        conn.execute("UPDATE jobs SET state='cancelled',updated=? WHERE id=?",(C.now(),draft['id']))


def storage_status(conn, uid):
    return capacity_snapshot(conn, C.FILES, uid, max_upload_bytes=C.LIMIT, reserve_bytes=C.RESERVE)


def member_capacity(uid):
    with C.db() as conn:
        conn.execute('BEGIN')
        return storage_status(conn, uid)


async def capacity(request):
    session = await authorize(request)
    snapshot = await blocking(member_capacity, session['uid'])
    # Only this participant's quota and a fleet-wide low-space flag are public.
    return web.json_response({
        'member_limit_bytes': snapshot['member_limit_bytes'],
        'member_low': snapshot['member_low'],
        'member_upload_bytes': max(0, snapshot['member_available_bytes'] - MAX_TRANSCRIPT_BYTES),
        'disk_low': snapshot['disk_low'],
    })


def admission(conn, uid, additional_bytes, *, retry=False):
    """Caller holds BEGIN IMMEDIATE through admission and reserve/requeue."""
    outstanding=conn.execute("SELECT COUNT(*) FROM jobs WHERE uid=? AND state IN ('uploading','queued','processing')",(uid,)).fetchone()[0]
    if outstanding>=3:
        return failure(409,'У вас 3 активные записи. Завершите или отмените незаконченные загрузки.')
    reason = admission_reason(storage_status(conn, uid), additional_bytes, retry=retry)
    if reason == 'member_quota':
        return failure(409, 'Личного лимита хранения недостаточно для этой записи и ее расшифровки.')
    if reason == 'disk_capacity':
        return failure(507, 'На сервере недостаточно места для записи и ее расшифровки.')
    return None


def create_job(s, proof, shape, fp, busy):
    name, size, language, content_id, ext = (shape[k] for k in ('filename','size','language','content_id','ext'))
    ident = proof['id']
    with C.db() as c:
        c.execute('BEGIN IMMEDIATE')
        expire_drafts(c, busy)
        existing = c.execute("""SELECT * FROM jobs WHERE uid=? AND chat=? AND thread IS ? AND reply IS ?
            AND filename=? AND size=? AND language=? AND content_id=? AND proof IS NOT NULL AND (state IN
            ('uploading','queued','processing','delivery_pending','sending') OR (state='done' AND updated>?))
            ORDER BY created DESC LIMIT 1""", (s['uid'],s['chat'],s['thread'],s['reply'],name,size,
                                               language,content_id,C.now()-86400)).fetchone()
        if existing:
            return web.json_response(public_job(existing))
        denied=admission(c,s['uid'],size)
        if denied is not None:return denied
        c.execute('INSERT INTO jobs(id,uid,chat,thread,reply,filename,ext,size,fingerprint,language,state,created,updated,content_id,proof) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (ident,s['uid'],s['chat'],s['thread'],s['reply'],name,ext,size,fp,language,'uploading',C.now(),C.now(),content_id,proof['proof']))
        p=C.file_path({'id':ident,'ext':ext})
        with p.open('xb'):
            pass
    return web.json_response({'id':ident,'offset':0}, status=201)


async def create(request):
    s = await authorize(request)
    if request.content_length is None or request.content_length > 4096:
        raise web.HTTPBadRequest()
    b = await request.json()
    size = b.get('size')
    name = Path(str(b.get('filename','')).replace('\\','/')).name
    name = ''.join(ch for ch in name if ch.isprintable())[:180]
    ext = Path(name).suffix.lower()
    language = b.get('language','ru')
    if type(size) is not int or not 0 < size <= C.LIMIT or ext not in C.EXTENSIONS or language not in ('ru','en','auto'):
        return failure(400, 'Выберите аудио или видео размером до 1 ГБ.')
    fp = b.get('fingerprint','')
    if not re.fullmatch('[a-f0-9]{64}', fp):
        raise web.HTTPBadRequest()
    if not re.fullmatch('[a-f0-9]{64}', str(b.get('content_id',''))):
        return failure(426, 'Откройте кнопку "Записи" заново, чтобы проверить целостность всего файла.')
    content_id = b['content_id']
    shape = dict(filename=name,ext=ext,size=size,language=language,content_id=content_id)
    proof = (await bound_request(request,s,'bind',shape=shape))['job']
    return await blocking(create_job, s, proof, shape, fp, busy_uploads())


def open_chunk(path, offset):
    """None if committed bytes are missing; otherwise a handle positioned after them."""
    f = path.open('r+b')
    try:
        if os.fstat(f.fileno()).st_size < offset:
            f.close()
            return None
        f.truncate(offset)
        f.seek(offset)
        return f
    except BaseException:
        f.close()
        raise


def seal_chunk(f, j, offset, received):
    f.flush()
    os.fsync(f.fileno())
    if offset==0:
        f.seek(0)
        actual=hashlib.sha256(f.read(min(65536,j['size']))).hexdigest()
        if actual!=j['fingerprint']:
            raise ValueError('Wrong recording')
    with C.db() as c:
        c.execute('UPDATE jobs SET offset=?,updated=? WHERE id=?',(offset+received,C.now(),j['id']))


def rewind(path, offset):
    with path.open('r+b') as f:
        f.truncate(offset)


async def append(request):
    s=await authorize(request)
    ident=request.match_info['id']
    await blocking(owned_job,ident,s)
    lock=locks.setdefault(ident,asyncio.Lock())
    async with lock:
        j=await blocking(owned_job,ident,s)
        if j['state']!='uploading':
            return failure(409,'Загрузка уже завершена.')
        length=request.content_length
        offset=int(request.headers.get('Upload-Offset','-1'))
        if offset!=j['offset']:
            return web.json_response({'error':'Сверьте позицию загрузки.','offset':j['offset']},status=409)
        if length is None or not 0<length<=C.CHUNK or offset+length>j['size']:
            return failure(413,'Недопустимый размер части файла.')
        if (await blocking(shutil.disk_usage,C.FILES)).free-length<C.RESERVE:
            return failure(507,'Недостаточно места для продолжения загрузки.')
        p=C.file_path(j)
        f=await asyncio.to_thread(open_chunk,p,offset)
        if f is None:
            return failure(409,'Файл поврежден. Начните новую загрузку.')
        received=0
        try:
            try:
                async for data in request.content.iter_chunked(256*1024):
                    received+=len(data)
                    if received>length:
                        raise ValueError('Excess data')
                    await asyncio.to_thread(f.write,data)
                if received!=length:
                    raise ValueError('Incomplete chunk')
                await blocking(seal_chunk,f,j,offset,received)
            finally:
                f.close()
        except BaseException:
            # Synchronous on purpose: it must also run when this task is being cancelled.
            rewind(p,offset)
            raise
        return web.json_response({'offset':offset+received})


def set_state(ident, sql, *args):
    with C.db() as c:
        c.execute(sql, (*args, ident))


def queued_state(ident, session):
    j = owned_job(ident, session)
    return public_job(j)


async def complete(request):
    s=await authorize(request);ident=request.match_info['id']
    await blocking(owned_job,ident,s)
    async with locks.setdefault(ident,asyncio.Lock()):
        j=await blocking(owned_job,ident,s)
        if j['state']!='uploading':
            return web.json_response(await blocking(public_job,j))
        if j['offset']!=j['size'] or (await blocking(C.file_path(j).stat)).st_size!=j['size']:
            return failure(409,'Сначала завершите загрузку файла.')
        if s['chat']==C.GROUP and not await membership(request,s['uid']):
            return failure(403,'Нет доступа к группе.')
        if not j['content_id']:
            return failure(426,'Откройте обновленную мини-апку и выберите исходный файл заново.')
        sha, actual = await asyncio.to_thread(C.file_hashes, C.file_path(j))
        if actual != j['content_id']:
            await blocking(set_state, ident, "UPDATE jobs SET state='cancelled',updated=? WHERE id=?", C.now())
            await blocking(C.file_path(j).unlink, missing_ok=True)
            return failure(422,'Контрольная сумма файла не совпала. Выберите исходную запись заново.')
        await blocking(set_state, ident, "UPDATE jobs SET state='queued',sha256=?,updated=?,queued_at_ns=? WHERE id=?",
                       sha, C.now(), time.time_ns())
    return web.json_response({'state':'queued'})


async def transcript(request):
    s=await authorize(request);j=await blocking(owned_job,request.match_info['id'],s)
    await bound_request(request,s,'session')
    await blocking(authorized_result,j,s)
    size = await blocking(receipts.body_size, j['id'])
    if not size:
        raise web.HTTPNotFound()
    # Stream bounded pieces; neither the server nor the database read lock waits for a slow client.
    response = web.StreamResponse(headers={**SECURITY_HEADERS, 'Content-Type':'text/plain; charset=utf-8',
                                           'Content-Disposition':'attachment; filename="transcript.txt"'})
    response.content_length = size
    await response.prepare(request)
    for offset in range(0, size, TRANSCRIPT_PIECE):
        try:
            piece = await blocking(receipts.read_body, j['id'], offset, TRANSCRIPT_PIECE)
        except (LookupError, sqlite3.Error) as error:
            # Removed or unreadable mid-download: cut the connection instead of a short "success".
            logging.warning('Transcript download interrupted: %s', type(error).__name__)
            if request.transport is not None:
                request.transport.close()
            return response
        await response.write(piece)
    await response.write_eof()
    return response



def retry_job(ident, s, generation, busy):
    with C.db() as c:
        c.execute('BEGIN IMMEDIATE')
        j=c.execute("SELECT * FROM jobs WHERE id=? AND uid=? AND chat=?",(ident,s['uid'],s['chat'])).fetchone()
        if not j:raise web.HTTPNotFound()
        if generation>j['retry_generation'] or j['state'] in ('uploading','cancelled'):raise web.HTTPConflict()
        if generation<j['retry_generation'] or j['state']!='failed':
            return web.json_response({'state':j['state']})
        expire_drafts(c, busy)
        denied=admission(c,s['uid'],0,retry=True)
        if denied is not None:return denied
        # A separate upload can already be processing the same verified recording.
        duplicate=c.execute("""SELECT id FROM jobs WHERE uid=? AND chat=? AND thread IS ? AND reply IS ?
            AND filename=? AND size=? AND language=? AND content_id=? AND state IN
            ('uploading','queued','processing','delivery_pending','sending') LIMIT 1""",
            tuple(j[k] for k in ('uid','chat','thread','reply','filename','size','language','content_id'))).fetchone()
        if duplicate:return failure(409,'Эта запись уже поставлена в очередь.')
        c.execute("""UPDATE jobs SET state='queued',error=NULL,attempts=0,next_try=0,
                  retry_generation=retry_generation+1,updated=?,queued_at_ns=? WHERE id=?""",
                  (C.now(),time.time_ns(),ident))
    return web.json_response({'state':'queued'})


async def retry(request):
    s=await authorize(request);ident=request.match_info['id']
    await blocking(owned_job,ident,s)
    generation=0
    if request.can_read_body:
        if request.content_length is None or request.content_length>512:raise web.HTTPBadRequest()
        payload=await request.json()
        generation=payload.get('retry_generation') if isinstance(payload,dict) else None
        if type(generation) is not int or generation<0:raise web.HTTPBadRequest()
    if s['chat']==C.GROUP and not await membership(request,s['uid']):raise web.HTTPForbidden()
    async with locks.setdefault(ident,asyncio.Lock()):
        return await blocking(retry_job, ident, s, generation, busy_uploads())


def reprocess_job(parent, session, bound, busy):
    with C.db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        original = conn.execute('SELECT * FROM jobs WHERE id=? AND uid=? AND chat=?',
                                (parent, session['uid'], session['chat'])).fetchone()
        if not original:
            raise web.HTTPNotFound()
        previous = conn.execute('SELECT * FROM jobs WHERE reprocess_of=?', (parent,)).fetchone()
        if previous:
            return web.json_response(public_job(previous))
        if original['state'] != 'done' or original['ext'] not in C.EXTENSIONS:
            return failure(409, 'Эту запись можно обработать заново после завершения расшифровки.')
        expire_drafts(conn, busy)
        denied = admission(conn, session['uid'], 0)
        if denied is not None:
            return denied
        source = C.file_path(original)
        try:
            source_stat = source.lstat()
        except FileNotFoundError:
            return failure(409, 'Исходный файл недоступен. Выберите его на устройстве для новой загрузки.')
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size != original['size']:
            return failure(409, 'Исходный файл изменился. Выберите запись на устройстве.')
        # One successor per finished version, including retries after a lost HTTP response
        # or a process death between linking the recording and committing the database.
        ident = bound['id']
        now = C.now()
        try:
            conn.execute("""INSERT INTO jobs(id,uid,chat,thread,reply,filename,ext,size,offset,
                            fingerprint,language,content_id,state,created,updated,queued_at_ns,reprocess_of,proof)
                            VALUES(:id,:uid,:chat,:thread,:reply,:filename,:ext,:size,:size,
                            :fingerprint,:language,:content_id,'queued',:now,:now,:clock,:parent,:proof)""",
                         {**bound,'fingerprint':original['fingerprint'],'now':now,'clock':time.time_ns(),'parent':parent})
        except sqlite3.IntegrityError:
            return failure(409, 'Эта запись уже обрабатывается. Откройте ее в списке записей.')
        target = C.file_path({'id':ident,'ext':original['ext']})
        try:
            os.link(source, target, follow_symlinks=False)
        except FileExistsError:
            pass
        linked = target.lstat()
        if not stat.S_ISREG(linked.st_mode) or not os.path.samestat(source_stat, linked):
            raise web.HTTPConflict()
        if linked.st_size != original['size']:
            raise web.HTTPConflict()
        return web.json_response({'id':ident,'state':'queued'}, status=201)


async def reprocess(request):
    session = await authorize(request)
    parent = request.match_info['id']
    await blocking(owned_job, parent, session)
    if session['chat'] == C.GROUP and not await membership(request, session['uid']):
        raise web.HTTPForbidden()
    original = await blocking(owned_job, parent, session)
    if original['state'] != 'done':
        return failure(409,'Запись еще обрабатывается.')
    origin,_ = await blocking(authorized_result, original, session)
    shape = {k:origin[k] for k in ('filename','ext','size','language','content_id')}
    bound = (await bound_request(request,session,'bind',shape=shape,parent=origin))['job']
    return await blocking(reprocess_job, parent, session, bound, busy_uploads())


async def redeliver(request):
    s=await authorize(request)
    data=await request.json()
    confirmed = isinstance(data,dict) and data.get('confirm_possible_duplicate') is True
    ident=request.match_info['id']
    async with locks.setdefault(ident,asyncio.Lock()):
        job=await blocking(owned_job,ident,s)
        if job['state']=='delivery_pending':
            return web.json_response({'state':'delivery_pending'})
        if (job['state']!='done' or job['delivery']=='sent'
                or (job['delivery']=='unknown' and not confirmed)):
            return failure(409,'Сначала проверьте, был ли файл доставлен.')
        origin,_ = await blocking(authorized_result,job,s)
        retry = (await bound_request(request,s,'redeliver',job=origin,confirmed=confirmed))['retry']
        await blocking(set_state, ident, """UPDATE jobs SET state='delivery_pending',delivery=NULL,attempts=0,
                    next_try=0,updated=?,retry_proof=? WHERE id=? AND state='done'""", C.now(), json.dumps(retry))
    return web.json_response({'state':'delivery_pending'})


async def cancel(request):
    s=await authorize(request);ident=request.match_info['id']
    await blocking(owned_job,ident,s)
    async with locks.setdefault(ident,asyncio.Lock()):
        j=await blocking(owned_job,ident,s)
        if j['state']!='uploading':raise web.HTTPConflict()
        await blocking(set_state, ident, "UPDATE jobs SET state='cancelled',updated=? WHERE id=?", C.now())
        await blocking(C.file_path(j).unlink, missing_ok=True)
    return web.json_response({'state':'cancelled'})


async def index(request):return web.FileResponse(ASSETS/'index.html')
async def asset(request):
    name=request.match_info['name']
    if name not in ('app.js','style.css'):raise web.HTTPNotFound()
    return web.FileResponse(ASSETS/name)
async def health(request):return web.json_response({'ok':True})


async def lifecycle(app):
    await blocking(C.initialize)
    app['members']={}
    yield


def create_app():
    app=web.Application(middlewares=[boundary],client_max_size=C.CHUNK+1024)
    app.cleanup_ctx.append(lifecycle)
    app.add_routes([web.get('/',index),web.get('/assets/{name}',asset),web.get('/healthz',health),
        web.post('/api/auth',auth),web.get('/api/capacity',capacity),
        web.get('/api/jobs',jobs),web.post('/api/jobs',create),
        web.patch('/api/jobs/{id}',append),web.post('/api/jobs/{id}/complete',complete),
        web.get('/api/jobs/{id}/transcript',transcript),web.post('/api/jobs/{id}/retry',retry),
        web.post('/api/jobs/{id}/reprocess',reprocess),
        web.post('/api/jobs/{id}/redeliver',redeliver),web.post('/api/jobs/{id}/cancel',cancel)])
    return app


if __name__=='__main__':
    os.umask(0o077);logging.basicConfig(level=logging.WARNING,format='%(levelname)s %(message)s')
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version=ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(TLS/'fullchain.pem',TLS/'privkey.pem')
    app=create_app()
    async def setup_reload(app):
        def reload_tls():
            try:tls.load_cert_chain(TLS/'fullchain.pem',TLS/'privkey.pem')
            except Exception as e:logging.error('TLS reload failed: %s',type(e).__name__)
        asyncio.get_running_loop().add_signal_handler(signal.SIGHUP,reload_tls)
    app.on_startup.append(setup_reload)
    web.run_app(app,host=os.environ.get('PSY_LISTEN_HOST','0.0.0.0'),port=int(os.environ.get('PSY_LISTEN_PORT','9443')),ssl_context=tls,access_log=None,print=None,shutdown_timeout=45)
