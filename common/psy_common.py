"""Helpers shared by the PSY gateway side, the upload services and the ops scripts.

Standard library only, Python 3.9 or newer. Installed once, root-owned and read-only for every other
account, at /usr/local/lib/psy-common/psy_common.py. Each consumer loads it by that explicit path
with importlib (the `_common` function they all carry) and never puts its folder on sys.path, so it
cannot shadow a module of the process that loads it. Only tests set PSY_COMMON_PATH to a repository
copy. Tools that run from a release checkout (ops/instance_env.py, ops/stage-observer.py,
tools/telegram_entry.py, gateway/configure_profile.py) load the copy shipped next to them, because
they also run before it is installed.

Nothing here logs. Exception texts name a setting, a line or an HTTP status, never a value, a token
or a request URL.
"""
import collections
import contextlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request

INSTALLED = Path('/usr/local/lib/psy-common/psy_common.py')


# ---------- instance settings ----------

# Telegram user, bot and chat IDs fit in 52 bits.
ID_LIMIT = 2 ** 52
INSTANCE_FILE = Path('/etc/psy/instance.env')
REQUIRED = ('PSY_OWNER_ID', 'PSY_GROUP_ID', 'PSY_BOT_ID', 'PSY_BOT_USERNAME', 'PSY_PUBLIC_URL')
Instance = collections.namedtuple('Instance', 'owner group bot bot_username public_url')


def telegram_id(raw, name, *, group=False):
    """A positive user or bot ID, or with group=True a negative group ID, spelled as Telegram spells
    it: ASCII digits, no plus sign, no leading zero."""
    pattern = r'-[1-9][0-9]{0,15}' if group else r'[1-9][0-9]{0,15}'
    if not isinstance(raw, str) or not re.fullmatch(pattern, raw) or abs(int(raw)) >= ID_LIMIT:
        raise ValueError('Invalid ' + name)
    return int(raw)


def bot_username(raw, name='PSY_BOT_USERNAME'):
    """A bot username without @: 5 to 32 letters, digits or underscores, ending in "bot"."""
    if not isinstance(raw, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{1,28}[Bb][Oo][Tt]', raw):
        raise ValueError(name + ' must contain the bot username without @')
    return raw


def https_origin(raw, name='PSY_PUBLIC_URL'):
    """'https://host[:port]' from an HTTPS origin written with at most one trailing slash. No user,
    path, query or fragment, no backslash, #, ?, space, control or non-ASCII character, port 1-65535."""
    error = ValueError(name + ' must be an HTTPS origin')
    if not isinstance(raw, str) or not raw.isascii() or any(c <= ' ' or c == '\x7f' or c in '\\#?' for c in raw):
        raise error
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port
    except ValueError:
        raise error from None
    if (parts.scheme != 'https' or not parts.hostname or parts.username is not None or parts.password is not None
            or parts.path not in ('', '/') or parts.query or parts.fragment or port == 0):
        raise error
    return 'https://' + parts.netloc


def instance(environ=None):
    """The identity every PSY process runs with, from the environment (systemd's EnvironmentFile=)."""
    environ = os.environ if environ is None else environ
    owner = telegram_id(environ.get('PSY_OWNER_ID', ''), 'PSY_OWNER_ID')
    group = telegram_id(environ.get('PSY_GROUP_ID', ''), 'PSY_GROUP_ID', group=True)
    bot = telegram_id(environ.get('PSY_BOT_ID', ''), 'PSY_BOT_ID')
    if bot == owner:
        raise ValueError('PSY_BOT_ID and PSY_OWNER_ID must differ')
    return Instance(owner, group, bot, bot_username(environ.get('PSY_BOT_USERNAME', '')),
                    https_origin(environ.get('PSY_PUBLIC_URL', '')))


class InstanceError(ValueError):
    """instance.env cannot be used as it is. The text names a line and a key, never a value."""


_KEY = re.compile(r'[A-Z][A-Z0-9_]*')
# No whitespace, quotes, #, $, backslash, backquote or shell control characters.
_VALUE = re.compile(r'[A-Za-z0-9_.:/@+,=%\[\]-]*')
# Read by the PSY processes themselves; production never sets them in instance.env.
TEST_ONLY = ('PSY_COMMON_PATH',)


def _listen_host(value):
    if not re.fullmatch(r'[0-9A-Fa-f.:]+|[A-Za-z0-9.-]+', value):
        raise ValueError('Invalid PSY_LISTEN_HOST')


def _listen_port(value):
    if not (value.isascii() and value.isdecimal() and 0 < int(value) < 65536):
        raise ValueError('Invalid PSY_LISTEN_PORT')


FORMATS = {
    'PSY_OWNER_ID': lambda value: telegram_id(value, 'PSY_OWNER_ID'),
    'PSY_GROUP_ID': lambda value: telegram_id(value, 'PSY_GROUP_ID', group=True),
    'PSY_BOT_ID': lambda value: telegram_id(value, 'PSY_BOT_ID'),
    'PSY_BOT_USERNAME': bot_username,
    'PSY_PUBLIC_URL': https_origin,
    'PSY_LISTEN_HOST': _listen_host,
    'PSY_LISTEN_PORT': _listen_port,
}


def parse_instance(text, *, complete=False):
    """Values of an instance.env that systemd (EnvironmentFile=), bash (`. file`) and python-dotenv all
    read the same way: comment lines, blank lines and KEY=value with a plain value. With complete=True
    the five identity values must be set and consistent, as every service requires."""
    values = {}
    for number, line in enumerate(text.split('\n'), 1):
        if '\r' in line:
            raise InstanceError(f'line {number}: Windows line ending')
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not _KEY.fullmatch(key):
            raise InstanceError(f'line {number}: expected KEY=value with no spaces around "=" and no "export"')
        value = value.rstrip(' \t')
        if '#' in value:
            raise InstanceError(f'line {number}: {key} has an inline comment; put comments on their own line')
        if not _VALUE.fullmatch(value):
            raise InstanceError(f'line {number}: {key} has quotes, spaces, $ or other characters read differently by systemd and bash')
        if key in values:
            raise InstanceError(f'line {number}: {key} is set twice')
        if key in TEST_ONLY:
            raise InstanceError(f'line {number}: {key} is for tests only; services load the installed shared module')
        check = FORMATS.get(key)
        if value and check:
            try:
                check(value)
            except ValueError:
                raise InstanceError(f'line {number}: {key} has an invalid format') from None
        values[key] = value
    if complete:
        missing = [key for key in REQUIRED if not values.get(key)]
        if missing:
            raise InstanceError('missing values: ' + ', '.join(missing))
        try:
            instance(values)
        except ValueError as error:
            raise InstanceError(str(error)) from None
    return values


def load_instance(path=INSTANCE_FILE, *, complete=False):
    try:
        return parse_instance(Path(path).read_bytes().decode('utf-8'), complete=complete)
    except UnicodeDecodeError:
        raise InstanceError('not UTF-8') from None


# ---------- files ----------

def read_regular(path, limit=1 << 20, *, tail=False):
    """The first (or with tail=True the last) `limit` bytes of a regular file. A link at the last
    path component is refused and a FIFO never blocks the reader: other accounts can plant either in
    the folders root and the services read from."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError('not a regular file')
        if tail:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - limit))
        return stream.read(limit)


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def atomic_output(path, mode=0o600, *, durable=True):
    """Yield a binary file that replaces `path` only when the block completes; on an error the old
    file stays as it was. The temporary file is new (O_EXCL, a random name next to `path`), so no
    planted name or link is written through, and the rename replaces a link at `path` instead of
    following it. durable=True also syncs the data and the directory entry before returning."""
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name[:64] + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as output:
            yield output
            output.flush()
            os.fchmod(output.fileno(), mode)
            if durable:
                os.fsync(output.fileno())
        os.replace(name, path)
        if durable:
            sync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def atomic_write(path, data, mode=0o600, *, durable=True):
    with atomic_output(path, mode, durable=durable) as output:
        output.write(data)


def env_value(text, key):
    """`key` from a profile .env the way Hermes ends up with it: python-dotenv keeps the last line for a
    repeated key; an empty line is skipped, so a stray `KEY=` cannot hide the real value. Surrounding
    spaces and quotes are removed. '' when only empty lines name the key, None when it is absent."""
    found = None
    for line in text.splitlines():
        name, separator, value = line.partition('=')
        if separator and name.strip() == key:
            value = value.strip().strip('"\'')
            if value or found is None:
                found = value
    return found


# ---------- SQLite ----------

def open_readonly(path, timeout=3):
    """A read-only connection that never creates files next to the database, for readers that do not
    own it (root's checks, a service with a read-only view). A WAL database that no process holds
    open has no -wal/-shm, and opening it read-only would try to create them; it is fully
    checkpointed then, so the file itself is read as immutable. Links and FIFOs are refused."""
    path = Path(path)
    header = read_regular(path, 100)
    if len(header) >= 20 and header[18] == 2 and not all(
            os.path.exists(str(path) + side) for side in ('-wal', '-shm')):
        return sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=timeout)
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=timeout)
    try:
        connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
        return connection
    except sqlite3.OperationalError:
        # The sidecars disappeared in between (the last writer closed): read the checkpointed file.
        connection.close()
        return sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=timeout)


# ---------- Telegram Bot API ----------

BOT_API = 'https://api.telegram.org/bot'
_TOKEN = r'[1-9][0-9]{5,14}:[A-Za-z0-9_-]{30,}'


def bot_token(raw, bot_id=None):
    """A bot token of the usual shape and, when bot_id is given, of that bot."""
    pattern = _TOKEN if bot_id is None else re.escape(str(bot_id)) + r':[A-Za-z0-9_-]{30,}'
    if not isinstance(raw, str) or not re.fullmatch(pattern, raw):
        raise ValueError('Invalid bot credential')
    return raw


class BotApiError(Exception):
    """A Bot API call that did not return ok. `reason` (http=401, network=URLError, api=400) is safe
    to log, and `kind` is its first part: an HTTP error status, no answer at all, or an answer with
    ok false. Neither ever contains the token or the request."""

    def __init__(self, reason, *, status=None, error_code=None, retry_after=None):
        super().__init__(reason)
        self.reason, self.status, self.error_code, self.retry_after = reason, status, error_code, retry_after
        self.kind = reason.partition('=')[0]


def _retry_after(answer):
    parameters = answer.get('parameters') if isinstance(answer, dict) else None
    value = parameters.get('retry_after') if isinstance(parameters, dict) else None
    return value if type(value) in (int, float) else None


def bot_call(token, method, payload=None, *, timeout, form=False):
    """POST one Bot API method and return its `result`. The payload goes as JSON, as a form with
    form=True, or as an empty body when there is none. Failures raise BotApiError."""
    if payload is None:
        data, headers = b'', {}
    elif form:
        data, headers = urllib.parse.urlencode(payload).encode(), {}
    else:
        data, headers = json.dumps(payload).encode(), {'Content-Type': 'application/json'}
    request = urllib.request.Request(BOT_API + token + '/' + method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as error:
        retry = None
        if error.code == 429:
            try:
                retry = _retry_after(json.loads(error.read(8192)))
            except (OSError, ValueError, AttributeError):
                pass
        raise BotApiError(f'http={error.code}', status=error.code, retry_after=retry) from None
    except (OSError, ValueError, http.client.HTTPException) as error:   # URLError is an OSError
        raise BotApiError('network=' + type(error).__name__) from None
    if not isinstance(answer, dict) or answer.get('ok') is not True:
        code = answer.get('error_code') if isinstance(answer, dict) else None
        raise BotApiError(f'api={code}', error_code=code, retry_after=_retry_after(answer))
    return answer.get('result')
