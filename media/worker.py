"""Durable local ASR and Telegram delivery. A confirmed rejection is safe to retry.

Transcription and delivery are separate loops: a decode that runs for hours never delays a
finished transcript or a member's "send again". Queued recordings are taken in member turns."""
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
import asr_client
from asr_client import ASRDeadline, ASRStalled
import psy_upload_common as C

POLL_SECONDS = 4
# How often a running decode checks whether its recording was deleted.
WATCH_SECONDS = 5
ASR_ATTEMPTS = 8
MAX_BACKOFF = 1800
BEFORE_DELIVERY = ('queued', 'processing')
DELIVERY = ('delivery_pending', 'sending')


class MembershipUnavailable(Exception):
    pass


async def bot_call(http, method, payload):
    token, base = C.bot_settings()
    async with http.post(base+token+'/'+method, data=payload) as response:
        return await response.json()


async def group_access(http, job):
    if job['chat'] != C.GROUP:
        return job['uid'] == C.OWNER and job['chat'] == C.OWNER
    if job['uid'] == C.OWNER:
        # The owner may have left the group and keeps the group destination regardless.
        return True
    try:
        result = await bot_call(http, 'getChatMember', {'chat_id':str(C.GROUP), 'user_id':str(job['uid'])})
    except Exception as error:
        raise MembershipUnavailable from error
    if not result.get('ok'):
        raise MembershipUnavailable
    return C.member_allowed(result.get('result', {}))


async def transcribe(job):
    if (not grants.valid_job(job) or job['ext'] not in C.EXTENSIONS
            or (job['chat'] != C.GROUP and (job['uid'] != C.OWNER or job['chat'] != C.OWNER))):
        raise ValueError('Unverified recording grant')
    # The worker never opens a path under the web-writable directory. ASR checks
    # the signed content digest on its own private audio snapshot before decoding.
    with tempfile.TemporaryDirectory(dir=receipts.root()) as directory:
        output = Path(directory) / 'result.txt'
        stored = {}

        async def persist(header):
            # Saved before the service is told it may delete its copy and journal.
            data = receipts.read_regular(output)
            receipts.save(job, header, data)
            stored['text_size'] = len(data)

        result = await asr_client.transcribe_recording(job, output, persist)
        if result.get('complete') is not True or 'text_size' not in stored:
            raise ValueError('Incomplete output')
        result['text_size'] = stored['text_size']
    return result


def move(job, before, after, **fields):
    with C.db() as c:
        return C.transition(c, job['id'], before, after, **fields)


def fail(job, error):
    move(job, BEFORE_DELIVERY, 'failed', error=error)


def finish(job, delivery, message_id=None):
    move(job, DELIVERY, 'done', delivery=delivery, message_id=message_id)


def retry_later(job, stage, delay=0, *, bounded=False):
    """Back off and keep the job. Only a crashing local ASR stops after ASR_ATTEMPTS tries,
    so one recording that kills the decoder cannot occupy it forever."""
    attempts = job['attempts'] + 1
    if bounded and attempts >= ASR_ATTEMPTS:
        fail(job, 'Сервис временно недоступен. Можно повторить обработку.')
        return
    wait = max(min(MAX_BACKOFF, 10*2**min(attempts-1, 8)), max(0,delay))
    move(job, DELIVERY if stage == 'delivery_pending' else BEFORE_DELIVERY, stage,
         attempts=attempts, next_try=C.now()+wait, queued_at_ns=time.time_ns())


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
            conn.execute("UPDATE jobs SET next_try=? WHERE id=? AND state='delivery_pending'",
                         (record['next_try'],job['id']))
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
    # Claim the job before the receipt: a member's deletion and this send cannot both win.
    if not move(job, 'delivery_pending', 'sending'):
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
        # Telegram asked to slow down. The transcript is ready, so keep trying without a cap.
        count = record['attempts'] + 1
        wait = max(min(MAX_BACKOFF, 10*2**min(count-1, 8)),int((result.get('parameters') or {}).get('retry_after') or 0))
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
        fail(job, 'У записи нет подтвержденного источника. Загрузите исходный файл заново.')
        return
    saved = receipts.get(job['id'])
    if saved:
        move(job, BEFORE_DELIVERY, 'delivery_pending')
        return
    try:
        if not await group_access(http,job):
            raise PermissionError('Access revoked')
        result = await transcribe(job)
    except ASRStalled:
        fail(job, 'Распознавание остановлено после 45 минут без прогресса. Можно повторить обработку. '
                  'Если часть записи обработана, бот продолжит с сохраненного места.')
        return
    except ASRDeadline:
        # Not an error: the journal holds every finished minute. Requeue behind other members.
        move(job, BEFORE_DELIVERY, 'queued', next_try=0, queued_at_ns=time.time_ns(), error=None)
        return
    except MembershipUnavailable:
        retry_later(job,'queued')
        return
    except (ConnectionError, FileNotFoundError) as exc:
        logging.warning('ASR temporarily unavailable: %s', type(exc).__name__)
        retry_later(job,'queued',bounded=True)
        return
    move(job, BEFORE_DELIVERY, 'delivery_pending', duration=result['duration'],
         speech_detected=result.get('speech_detected'), attempts=0, next_try=0,
         text_size=result['text_size'], sha256=result.get('sha256'))


def next_ready_job(conn):
    """The oldest ready recording of the member whose last decode started longest ago."""
    return conn.execute("""SELECT * FROM jobs j WHERE state='queued' AND next_try<=?
        ORDER BY coalesce((SELECT max(p.started_at) FROM jobs p WHERE p.uid=j.uid),0),
        coalesce(queued_at_ns,updated*1000000000),created,id LIMIT 1""",(C.now(),)).fetchone()


def next_delivery(conn):
    return conn.execute("""SELECT * FROM jobs WHERE state='delivery_pending' AND next_try<=?
        ORDER BY coalesce(queued_at_ns,updated*1000000000),created,id LIMIT 1""",(C.now(),)).fetchone()


def claim_next():
    with C.db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = next_ready_job(c)
        if row is None:
            return None
        job = dict(row)
        if not C.transition(c, job['id'], 'queued', 'processing', started_at=C.now()):
            return None
    job['state'] = 'processing'
    return job


def job_state(ident):
    with C.db() as c:
        row = c.execute('SELECT state FROM jobs WHERE id=?', (ident,)).fetchone()
    return row['state'] if row else None


async def supervise(http, job):
    """Run one decode; stop it as soon as the member deletes the recording."""
    task = asyncio.ensure_future(run_job(http, job))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=WATCH_SECONDS)
            if not task.done() and job_state(job['id']) != 'processing':
                # Closing the ASR connection makes the service abort the orphaned decoder.
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return
        task.result()
    except asyncio.CancelledError:
        task.cancel()
        raise
    except Exception as error:
        logging.error('Job failed: %s',type(error).__name__)
        fail(job, 'Не удалось завершить расшифровку. Можно повторить обработку.')


async def transcription_loop(http):
    while True:
        try:
            job = claim_next()
        except Exception as error:
            logging.error('Queue unavailable: %s', type(error).__name__)
            job = None
        if job is None:
            await asyncio.sleep(POLL_SECONDS)
            continue
        await supervise(http, job)


async def delivery_step(http):
    with C.db() as c:
        row = next_delivery(c)
    if row is None:
        return False
    job = dict(row)
    try:
        await deliver(http, job)
    except Exception as error:
        logging.error('Delivery failed: %s',type(error).__name__)
        finish(job, 'unknown' if job_state(job['id']) == 'sending' else 'failed')
    return True


async def delivery_loop(http):
    while True:
        try:
            busy = await delivery_step(http)
        except Exception as error:
            logging.error('Delivery queue unavailable: %s', type(error).__name__)
            busy = False
        if not busy:
            await asyncio.sleep(POLL_SECONDS)


async def main():
    C.initialize()
    receipts.initialize()
    with receipts.db(True) as db:
        db.execute("UPDATE results SET delivery='unknown' WHERE delivery='sending'")
    with C.db() as c:
        # This process is the only decoder client; its previous connection is gone.
        c.execute("UPDATE jobs SET state='queued' WHERE state='processing'")
        c.execute("UPDATE jobs SET state='delivery_pending',next_try=0 WHERE state='sending'")
    async with ClientSession(timeout=ClientTimeout(total=120)) as http:
        await asyncio.gather(transcription_loop(http), delivery_loop(http))


if __name__=='__main__':
    os.umask(0o077)
    logging.basicConfig(level=logging.WARNING,format='%(levelname)s %(message)s')
    asyncio.run(main())
