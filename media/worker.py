"""Durable local ASR and Telegram delivery. A confirmed rejection is safe to retry."""
import asyncio
import json
import logging
import os
import time
import tempfile
from pathlib import Path
import io
import recording_grant as grants
import recording_receipts as receipts

from aiohttp import ClientSession, ClientTimeout, FormData
from asr_client import ASRStalled
import psy_upload_common as C


class MembershipUnavailable(Exception):
    pass


async def bot_call(http, method, payload):
    token, base = C.bot_settings()
    async with http.post(base+token+'/'+method, data=payload) as response:
        return await response.json()


async def group_access(http, job):
    if job['chat'] != C.GROUP:
        return job['uid'] == C.OWNER and job['chat'] == C.OWNER
    try:
        result = await bot_call(http, 'getChatMember', {'chat_id':str(C.GROUP), 'user_id':str(job['uid'])})
    except Exception as error:
        raise MembershipUnavailable from error
    if not result.get('ok'):
        raise MembershipUnavailable
    return C.member_allowed(result.get('result', {}))


async def transcribe(job):
    from asr_client import transcribe_recording
    if (not grants.valid_job(job) or job['ext'] not in C.EXTENSIONS
            or (job['chat'] != C.GROUP and (job['uid'] != C.OWNER or job['chat'] != C.OWNER))):
        raise ValueError('Unverified recording grant')
    # The worker never opens a path under the web-writable directory. ASR checks
    # the signed content digest on its own private audio snapshot before decoding.
    with tempfile.TemporaryDirectory(dir=receipts.root()) as directory:
        output = Path(directory) / 'result.txt'
        result = await transcribe_recording(job, output)
        if result.get('complete') is not True:
            raise ValueError('Incomplete output')
        data = receipts.read_regular(output)
        receipts.save(job,result,data)
        result['text_size'] = len(data)
    return result


def finish(job, delivery, message_id=None):
    with C.db() as c:
        c.execute("UPDATE jobs SET state='done',delivery=?,message_id=?,updated=? WHERE id=?",
                  (delivery,message_id,C.now(),job['id']))


def retry_later(job, stage, delay=0):
    attempts = job['attempts'] + 1
    if attempts >= 8:
        if stage == 'delivery_pending':
            finish(job, 'failed')
        else:
            with C.db() as c:
                c.execute("UPDATE jobs SET state='failed',error=?,updated=? WHERE id=?",
                          ('Сервис временно недоступен. Можно повторить обработку.',C.now(),job['id']))
        return
    wait = max(min(1800, 10*2**(attempts-1)), max(0,delay))
    with C.db() as c:
        c.execute('UPDATE jobs SET state=?,attempts=?,next_try=?,updated=?,queued_at_ns=? WHERE id=?',
                  (stage,attempts,C.now()+wait,C.now(),time.time_ns(),job['id']))


async def deliver(http, job):
    record = receipts.get(job['id'],body=True)
    if record is None:
        finish(job,'unverified')
        return
    origin = json.loads(record['binding'])
    if not grants.valid_job(origin) or any(origin[k]!=job[k] for k in grants.ROUTE):
        # The projection is writable by ingress. Reject rerouting even to an allowed chat.
        finish(job,'unverified')
        return
    state = record['delivery']
    retry = json.loads(job['retry_proof']) if job.get('retry_proof') else None
    if state == 'sent' or (state != 'ready' and not retry):
        finish(job,'unknown' if state == 'sending' else state,record['message_id'])
        return
    if state == 'ready' and record['next_try'] > C.now():
        with C.db() as conn:
            conn.execute('UPDATE jobs SET next_try=? WHERE id=?',(record['next_try'],job['id']))
        return
    try:
        allowed = await group_access(http, origin)
    except MembershipUnavailable:
        retry_later(job,'delivery_pending')
        return
    if not allowed:
        receipts.status(job['id'],'access_revoked')
        finish(job,'access_revoked')
        return
    if not receipts.start(job['id'],C.now(),retry):
        finish(job,'unknown' if state == 'sending' else state,record['message_id'])
        return
    origin['speech_detected'] = record['speech']
    form = FormData()
    form.add_field('chat_id',str(origin['chat']))
    if origin['speech_detected'] == 0:
        caption = 'Обработка завершена. Разборчивая речь в записи не обнаружена.'
    else:
        caption = 'Расшифровка записи "'+origin['filename'][:150]+'" готова. Ответьте на это сообщение, чтобы обсудить текст.'
    form.add_field('caption',caption)
    if origin['thread']:
        form.add_field('message_thread_id',str(origin['thread']))
    if origin['reply']:
        form.add_field('reply_parameters',json.dumps({'message_id':origin['reply'],'allow_sending_without_reply':True}))
    with io.BytesIO(record['body']) as text_file:
        form.add_field('document',text_file,filename='Расшифровка.txt',content_type='text/plain; charset=utf-8')
        # Persist the ambiguous boundary before doing the non-idempotent HTTP call.
        with C.db() as c:
            c.execute("UPDATE jobs SET state='sending',updated=? WHERE id=?", (C.now(),job['id']))
        try:
            result = await bot_call(http,'sendDocument',form)
        except Exception:
            receipts.status(job['id'],'unknown')
            finish(job,'unknown')
            return
    if result.get('ok') and type((result.get('result') or {}).get('message_id')) is int:
        msg = result['result']['message_id']
        receipts.status(job['id'],'sent',msg)
        finish(job,'sent',msg)
    elif result.get('ok') is False and result.get('error_code') == 429:
        count = record['attempts'] + 1
        if count >= 8:
            receipts.status(job['id'],'failed')
            finish(job,'failed')
            return
        wait = max(10*2**(count-1),int((result.get('parameters') or {}).get('retry_after') or 0))
        receipts.status(job['id'],'ready',next_try=C.now()+wait,attempt=count)
        retry_later({**job,'attempts':count-1},'delivery_pending',wait)
    else:
        state = 'failed' if result.get('ok') is False and result.get('error_code',0) in range(400,500) else 'unknown'
        receipts.status(job['id'],state)
        finish(job,state)



async def run_job(http, job):
    if job['state'] == 'delivery_pending':
        await deliver(http,job)
        return
    if not grants.valid_job(job):
        with C.db() as conn:
            conn.execute("UPDATE jobs SET state='failed',error=?,updated=? WHERE id=?",
                         ('У записи нет подтвержденного источника. Загрузите исходный файл заново.',C.now(),job['id']))
        return
    saved = receipts.get(job['id'])
    if saved:
        with C.db() as conn:
            conn.execute("UPDATE jobs SET state='delivery_pending',updated=? WHERE id=?",(C.now(),job['id']))
        return
    try:
        if not await group_access(http,job):
            raise PermissionError('Access revoked')
        result = await transcribe(job)
    except ASRStalled:
        with C.db() as c:
            c.execute("UPDATE jobs SET state='failed',error=?,updated=? WHERE id=?", (
                'Распознавание остановлено после 45 минут без прогресса. Можно повторить обработку. '
                'Если часть записи обработана, бот продолжит с сохраненного места.', C.now(), job['id']))
        return
    except (MembershipUnavailable, ConnectionError, FileNotFoundError) as exc:
        logging.warning('ASR temporarily unavailable: %s', type(exc).__name__)
        retry_later(job,'queued')
        return
    with C.db() as c:
        c.execute("""UPDATE jobs SET state='delivery_pending',duration=?,speech_detected=?,
                     attempts=0,next_try=0,updated=?,text_size=?,sha256=? WHERE id=?""",
                  (result['duration'],result.get('speech_detected'),C.now(),result['text_size'],result.get('sha256'),job['id']))


def next_ready_job(conn):
    return conn.execute("""SELECT * FROM jobs WHERE state IN ('queued','delivery_pending')
        AND next_try<=? ORDER BY (state='delivery_pending') DESC,
        coalesce(queued_at_ns,updated*1000000000),created,id LIMIT 1""",(C.now(),)).fetchone()


async def main():
    C.initialize()
    receipts.initialize()
    with receipts.db(True) as db:
        db.execute("UPDATE results SET delivery='unknown' WHERE delivery='sending'")
    with C.db() as c:
        c.execute("UPDATE jobs SET state='queued' WHERE state='processing'")
        c.execute("UPDATE jobs SET state='delivery_pending',next_try=0 WHERE state='sending'")
    async with ClientSession(timeout=ClientTimeout(total=120)) as http:
        while True:
            with C.db() as c:
                c.execute('BEGIN IMMEDIATE')
                row = next_ready_job(c)
                job = dict(row) if row else None
                if job and job['state']=='queued':
                    c.execute("UPDATE jobs SET state='processing',updated=? WHERE id=?",(C.now(),job['id']))
            if not job:
                await asyncio.sleep(4)
                continue
            try:
                await run_job(http,job)
            except Exception as error:
                logging.error('Job failed: %s',type(error).__name__)
                if job['state']=='delivery_pending':
                    with C.db() as c:
                        state = c.execute('SELECT state FROM jobs WHERE id=?',(job['id'],)).fetchone()['state']
                    finish(job,'unknown' if state=='sending' else 'failed')
                else:
                    with C.db() as c:
                        c.execute("UPDATE jobs SET state='failed',error=?,updated=? WHERE id=?",
                                  ('Не удалось завершить расшифровку. Можно повторить обработку.',C.now(),job['id']))


if __name__=='__main__':
    os.umask(0o077)
    logging.basicConfig(level=logging.WARNING,format='%(levelname)s %(message)s')
    asyncio.run(main())
