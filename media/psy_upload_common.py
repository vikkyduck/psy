"""Private durable state shared by the uploader, the delivery worker and the narrow Telegram handoff."""
import contextlib
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import recording_grant as grants
import sqlite3
import threading
import time
from urllib.parse import parse_qsl, urlsplit
from storage_capacity import MAX_UPLOAD_BYTES, SYSTEM_RESERVE_BYTES

def telegram_id(name, *, group=False):
    raw = os.environ[name]
    pattern = r'-[1-9][0-9]*' if group else r'[1-9][0-9]*'
    if not re.fullmatch(pattern, raw) or abs(int(raw)) >= 2**52:
        raise ValueError(f'Invalid {name}')
    return int(raw)


def https_origin(raw):
    parsed = urlsplit(raw)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ('', '/') or parsed.query
            or parsed.fragment or not 0 < (parsed.port if parsed.port is not None else 443) <= 65535
            or not raw.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in raw)
            or chr(92) in raw):
        raise ValueError('PSY_PUBLIC_URL must be an HTTPS origin')
    return 'https://' + parsed.netloc


OWNER = telegram_id('PSY_OWNER_ID')
GROUP = telegram_id('PSY_GROUP_ID', group=True)
BOT_ID = telegram_id('PSY_BOT_ID')
BOT_USERNAME = os.environ['PSY_BOT_USERNAME']
if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]{4,31}', BOT_USERNAME) or not BOT_USERNAME.lower().endswith('bot'):
    raise ValueError('PSY_BOT_USERNAME must contain the bot username without @')
if BOT_ID == OWNER:
    raise ValueError('PSY_BOT_ID and PSY_OWNER_ID must differ')
PUBLIC_URL = https_origin(os.environ['PSY_PUBLIC_URL'])
HOME = Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes'))
ROOT = Path(os.environ.get('PSY_UPLOAD_ROOT', HOME / 'vault' / 'uploads'))
FILES = Path(os.environ.get('PSY_UPLOAD_FILES', HOME / 'media-vault' / 'uploads'))
LIMIT = MAX_UPLOAD_BYTES
CHUNK = 4 * 1024 ** 2
RESERVE = SYSTEM_RESERVE_BYTES
HASH_BLOCK = 1024 * 1024
DRAFT_TTL = 7 * 86400
EXTENSIONS = {'.mp3', '.wav', '.m4a', '.aac', '.ogg', '.opus', '.flac', '.mp4', '.mov', '.webm', '.mkv', '.mpga', '.mpeg', '.wma', '.aiff', '.caf'}


def now():
    return int(time.time())


# Bump with a new step in MIGRATIONS. Version 0 is any database created before versioning.
SCHEMA_VERSION = 2


def _columns(c, table):
    return {r['name'] for r in c.execute('PRAGMA table_info('+table+')')}


def _baseline(c):
    """Schema of every installation before PRAGMA user_version, including partial upgrades."""
    c.execute("""CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY, uid INTEGER NOT NULL,
        chat INTEGER NOT NULL, thread INTEGER, reply INTEGER, expires INTEGER NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY, uid INTEGER NOT NULL,
        chat INTEGER NOT NULL, thread INTEGER, reply INTEGER, expires INTEGER NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, uid INTEGER NOT NULL, chat INTEGER NOT NULL,
        thread INTEGER, reply INTEGER, filename TEXT NOT NULL, ext TEXT NOT NULL, size INTEGER NOT NULL,
        offset INTEGER NOT NULL DEFAULT 0, fingerprint TEXT NOT NULL, language TEXT NOT NULL,
        state TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL,
        error TEXT, sha256 TEXT, duration REAL, delivery TEXT, message_id INTEGER)""")
    c.execute('CREATE INDEX IF NOT EXISTS job_owner ON jobs(uid, chat, created)')
    for table in ('sessions', 'tickets'):
        if 'proof' not in _columns(c, table):
            c.execute('ALTER TABLE '+table+' ADD COLUMN proof TEXT')
    columns = _columns(c, 'jobs')
    for column, definition in (
        ('content_id', 'TEXT'), ('speech_detected', 'INTEGER'),
        ('attempts', 'INTEGER NOT NULL DEFAULT 0'), ('next_try', 'INTEGER NOT NULL DEFAULT 0'),
        ('retry_generation', 'INTEGER NOT NULL DEFAULT 0'),
        ('queued_at_ns', 'INTEGER'),
        ('reprocess_of', 'TEXT'), ('proof','TEXT'), ('retry_proof','TEXT'), ('text_size','INTEGER'),
    ):
        if column not in columns:
            c.execute(f'ALTER TABLE jobs ADD COLUMN {column} {definition}')
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS active_content ON jobs
        (uid,chat,ifnull(thread,0),ifnull(reply,0),filename,size,language,content_id)
        WHERE content_id IS NOT NULL AND state IN ('uploading','queued','processing','delivery_pending','sending')""")
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS reprocess_parent ON jobs(reprocess_of) WHERE reprocess_of IS NOT NULL')


def _worker_turns(c):
    # started_at gives each member a turn on the single decoder. job_ready serves both
    # worker polls and draft expiry; job_turn answers "when did this member last start".
    if 'started_at' not in _columns(c, 'jobs'):
        c.execute('ALTER TABLE jobs ADD COLUMN started_at INTEGER')
    c.execute('CREATE INDEX IF NOT EXISTS job_ready ON jobs(state, next_try)')
    c.execute('CREATE INDEX IF NOT EXISTS job_turn ON jobs(uid, started_at)')


MIGRATIONS = ((1, _baseline), (2, _worker_turns))


def migrate(c):
    """The only schema writer. Every process may call it; the version check makes repeats free."""
    if c.execute('PRAGMA user_version').fetchone()[0] >= SCHEMA_VERSION:
        return False
    c.execute('BEGIN IMMEDIATE')
    version = c.execute('PRAGMA user_version').fetchone()[0]
    for target, step in MIGRATIONS:
        if version < target:
            step(c)
    if version < SCHEMA_VERSION:
        c.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
    return True


def _write_ahead(c):
    """Readers never block the writer and commits avoid most fsyncs. Persistent in the file."""
    if c.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal':
        return True
    try:
        return c.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() == 'wal'
    except sqlite3.OperationalError:
        # Another process holds the database. The next start converts it.
        logging.warning('Upload state database kept its rollback journal for now')
        return False


def initialize():
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    FILES.mkdir(parents=True, exist_ok=True, mode=0o700)
    with db() as c:
        migrate(c)
    with db() as c:
        if _write_ahead(c):
            c.execute('PRAGMA synchronous=NORMAL')


def transition(c, ident, before, after, **fields):
    """Move one job from an expected state. False if another process changed it first."""
    before = (before,) if isinstance(before, str) else tuple(before)
    names = ''.join(f',{name}=?' for name in fields)
    cursor = c.execute(f"UPDATE jobs SET state=?,updated=?{names} WHERE id=? AND state IN ({','.join('?'*len(before))})",
                       (after, now(), *fields.values(), ident, *before))
    return cursor.rowcount == 1


def file_hashes(path):
    """Return the full SHA-256 and the browser-compatible digest of ordered 1 MiB block digests."""
    full, blocks = hashlib.sha256(), hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(HASH_BLOCK), b''):
            full.update(data)
            blocks.update(hashlib.sha256(data).digest())
    return full.hexdigest(), blocks.hexdigest()


_pool = threading.local()


def _connect(path):
    c = sqlite3.connect(path, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA secure_delete=ON')
    # NORMAL is durable against process crashes in WAL mode; keep FULL for a rollback journal.
    if c.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal':
        c.execute('PRAGMA synchronous=NORMAL')
    return c


def _discard(entry):
    try:
        entry[1].close()
    except sqlite3.Error:
        pass


@contextlib.contextmanager
def db():
    """One reused connection per thread. Nested uses share the outermost transaction."""
    path = str(ROOT / 'state.sqlite3')
    entry = getattr(_pool, 'entry', None)
    if entry is None or entry[0] != path:
        if entry is not None and not entry[2]:
            _discard(entry)
        entry = _pool.entry = [path, _connect(path), 0]
    c = entry[1]
    entry[2] += 1
    try:
        yield c
        if entry[2] == 1 and c.in_transaction:
            c.commit()
    except BaseException:
        if entry[2] == 1 and c.in_transaction:
            try:
                c.rollback()
            except sqlite3.Error:
                _pool.entry = None
                _discard(entry)
        raise
    finally:
        entry[2] -= 1


def ticket(uid, chat, thread=None, reply=None):
    if chat != GROUP and not (uid == OWNER and chat == OWNER):
        raise ValueError('Destination is not allowed')
    ident = secrets.token_hex(16)
    with db() as c:
        c.execute('DELETE FROM tickets WHERE expires < ?', (now(),))
        row = dict(id=ident,uid=uid,chat=chat,thread=thread,reply=reply,expires=now()+3600)
        row['proof'] = grants.sign('ticket',grants.ticket_fields(row))
        c.execute('INSERT INTO tickets(id,uid,chat,thread,reply,expires,proof) VALUES(:id,:uid,:chat,:thread,:reply,:expires,:proof)',row)
    return ident


def get_ticket(ident, uid):
    if not isinstance(ident, str) or not re.fullmatch('[a-f0-9]{32}', ident):
        return None
    with db() as c:
        r = c.execute('SELECT * FROM tickets WHERE id=? AND uid=? AND expires>?', (ident, uid, now())).fetchone()
        return dict(r) if r else None


INIT_MAX_AGE = 600
INIT_MAX_SKEW = 30


def init_fields(raw, current=None, *, future_slack=0):
    """Every launch-data check except the bot signature, shared by the web server and the broker.

    The web server runs it before contacting the broker, so it must never reject launch data
    that the broker would accept. future_slack only widens the clock-skew bound for a caller
    that checks earlier than the broker does."""
    if not isinstance(raw, str) or not 1 <= len(raw) <= 16384:
        raise ValueError('Invalid authentication')
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    if len({k for k, _ in pairs}) != len(pairs):
        raise ValueError('Duplicate authentication field')
    fields = dict(pairs)
    # The broker compares with a lowercase SHA-256 hexdigest, so nothing else can verify.
    if not re.fullmatch('[0-9a-f]{64}', fields.get('hash', '')):
        raise ValueError('Invalid signature')
    age = (now() if current is None else current) - int(fields.get('auth_date', '0'))
    if not -INIT_MAX_SKEW - future_slack <= age <= INIT_MAX_AGE:
        raise ValueError('Authentication expired')
    try:
        user = json.loads(fields['user'])
    except RecursionError:
        raise ValueError('Invalid user') from None
    if type(user.get('id')) is not int or user['id'] <= 0 or user.get('is_bot'):
        raise ValueError('Invalid user')
    return fields, user


def plausible_init(raw, current=None):
    """Cheap local shape check; the signature is still verified only by the broker."""
    try:
        init_fields(raw, current, future_slack=INIT_MAX_SKEW)
    except (ValueError, KeyError, TypeError, AttributeError):
        return False
    return True


def validate_init_context(raw, token, current=None):
    fields, user = init_fields(raw, current)
    supplied = fields.pop('hash')
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, '\n'.join(f'{k}={v}' for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise ValueError('Invalid signature')
    return {'uid': user['id'], 'start_param': fields.get('start_param', '')}


def validate_init(raw, token, current=None):
    return validate_init_context(raw, token, current)['uid']


def store_session(row):
    with db() as c:
        # Only expiry removes a session: opening the Mini App again never logs out another window.
        c.execute('DELETE FROM sessions WHERE expires < ?', (now(),))
        c.execute('INSERT INTO sessions(hash,uid,chat,thread,reply,expires,proof) VALUES(:hash,:uid,:chat,:thread,:reply,:expires,:proof)',row)



def get_session(secret):
    if not secret or len(secret) > 128:
        return None
    with db() as c:
        r = c.execute('SELECT * FROM sessions WHERE hash=? AND expires>?', (hashlib.sha256(secret.encode()).hexdigest(), now())).fetchone()
        return dict(r) if r else None


def file_path(job, suffix=None):
    ident = job['id']
    if not re.fullmatch('[a-f0-9]{32}', ident):
        raise ValueError('Invalid job')
    ext = suffix or job['ext']
    if ext not in EXTENSIONS and ext != '.txt':
        raise ValueError('Invalid extension')
    return FILES / (ident + ext)


def bot_settings():
    credentials = Path(os.environ['CREDENTIALS_DIRECTORY'])
    token = (credentials / 'telegram').read_text().strip()
    if not re.fullmatch(rf'{BOT_ID}:[A-Za-z0-9_-]{{30,}}', token):
        raise RuntimeError('Telegram credential missing or belongs to a different bot')
    return token, 'https://api.telegram.org/bot'


def member_allowed(member):
    return member.get('status') in ('creator', 'administrator', 'member') or (member.get('status') == 'restricted' and member.get('is_member') is True)
