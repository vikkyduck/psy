"""Domain-separated grants. Only gateway, access broker and delivery worker receive the key."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time

ROUTE = ('uid', 'chat', 'thread', 'reply')
FIELDS = (*ROUTE, 'filename', 'ext', 'size', 'language', 'content_id', 'id')
TTL = 8 * 3600


def key():
    value = (Path(os.environ['CREDENTIALS_DIRECTORY']) / 'recording-signing').read_bytes()
    if len(value) != 32:
        raise ValueError('Invalid recording credential')
    return value


def sign(kind, fields):
    body = json.dumps(fields, ensure_ascii=True, separators=(',', ':'), sort_keys=True)
    return hmac.new(key(), (kind + '\n' + body).encode(), hashlib.sha256).hexdigest()


def checked(kind, fields, proof):
    return isinstance(proof, str) and len(proof) == 64 and hmac.compare_digest(sign(kind, fields), proof)


def route(row):
    return {k: row.get(k) for k in ROUTE}


def ticket_fields(row):
    return {**route(row), 'id': row['id'], 'expires': row['expires']}


def session_fields(row):
    return {**route(row), 'hash': row['hash'], 'expires': row['expires']}


def session(secret, context, expires):
    fields = {**route(context), 'hash': hashlib.sha256(secret.encode()).hexdigest(), 'expires': expires}
    return {**fields, 'proof': sign('session', fields)}


def valid_session(row):
    return (isinstance(row, dict) and type(row.get('expires')) is int and time.time() < row['expires']
            and checked('session', session_fields(row), row.get('proof')))


def job_fields(row):
    return {k: row[k] for k in FIELDS}


def valid_job(row):
    return checked('job', job_fields(row), row.get('proof'))


def bind(context, shape, parent=None):
    if not valid_session(context):
        raise PermissionError()
    dest = route(context)
    if parent is not None:
        if not valid_job(parent) or any(parent[k] != context[k] for k in ('uid', 'chat')):
            raise PermissionError()
        if any(shape[k] != parent[k] for k in ('filename','ext','size','language','content_id')):
            raise PermissionError()
        dest = route(parent)
    fields = {**dest, **{k: shape[k] for k in ('filename','ext','size','language','content_id')},
              'id': secrets.token_hex(16)}
    if (not isinstance(fields['filename'], str) or len(fields['filename']) > 180
            or not isinstance(fields['ext'], str) or not re.fullmatch(r'\.[a-z0-9]{1,8}',fields['ext'])
            or fields['language'] not in ('ru','en','auto') or type(fields['size']) is not int
            or not 0 < fields['size'] <= 1024**3 or not re.fullmatch('[a-f0-9]{64}', fields['content_id'])):
        raise ValueError()
    return {**fields, 'proof': sign('job', fields)}


def redelivery(context, job, confirmed):
    if (not valid_session(context) or not valid_job(job) or
            any(job[k] != context[k] for k in ('uid', 'chat'))):
        raise PermissionError()
    fields = {'id': job['id'], 'nonce': secrets.token_hex(16), 'confirmed': confirmed is True}
    return {**fields, 'proof': sign('redeliver', fields)}
