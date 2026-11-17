"""The one reader of /etc/psy/instance.env for ops scripts.

systemd (EnvironmentFile=), bash (`. file`) and python-dotenv disagree on quotes, inline comments,
spaces and $: `PSY_LISTEN_PORT=9443 # port` is "9443" to bash and dotenv but "9443 # port" to
systemd. Only lines all three read the same way are accepted: comment lines starting with #,
blank lines and KEY=value with a plain value. Errors name the line and key, never the value.

  instance_env.py check [--complete] [FILE]     validate (and with --complete require the IDs)
  instance_env.py get KEY [FILE]                print one value
  instance_env.py exec [FILE] -- COMMAND...     run COMMAND with the values in its environment
"""
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

DEFAULT = Path('/etc/psy/instance.env')
KEY = re.compile(r'[A-Z][A-Z0-9_]*')
# No whitespace, quotes, #, $, backslash, backquote or shell control characters.
VALUE = re.compile(r'[A-Za-z0-9_.:/@+,=%\[\]-]*')
REQUIRED = ('PSY_OWNER_ID', 'PSY_GROUP_ID', 'PSY_BOT_ID', 'PSY_BOT_USERNAME', 'PSY_PUBLIC_URL')


class InstanceError(ValueError):
    pass


def https_origin(value):
    parts = urlsplit(value)
    if (parts.scheme != 'https' or not parts.hostname or parts.username is not None or parts.password is not None
            or parts.path not in ('', '/') or parts.query or parts.fragment):
        raise ValueError
    _ = parts.port   # raises on a bad port
    return True


FORMATS = {
    'PSY_OWNER_ID': lambda v: v.isdecimal() and 0 < int(v) < 2**52,
    'PSY_GROUP_ID': lambda v: v.startswith('-') and v[1:].isdecimal() and 0 < int(v[1:]) < 2**52,
    'PSY_BOT_ID': lambda v: v.isdecimal() and 0 < int(v) < 2**52,
    'PSY_BOT_USERNAME': lambda v: re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', v) is not None,
    'PSY_PUBLIC_URL': https_origin,
    'PSY_LISTEN_HOST': lambda v: re.fullmatch(r'[0-9A-Fa-f.:]+|[A-Za-z0-9.-]+', v) is not None,
    'PSY_LISTEN_PORT': lambda v: v.isdecimal() and 0 < int(v) < 65536,
}


def parse(text, *, complete=False):
    values = {}
    for number, line in enumerate(text.split('\n'), 1):
        if '\r' in line:
            raise InstanceError(f'line {number}: Windows line ending')
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not KEY.fullmatch(key):
            raise InstanceError(f'line {number}: expected KEY=value with no spaces around "=" and no "export"')
        value = value.rstrip(' \t')
        if '#' in value:
            raise InstanceError(f'line {number}: {key} has an inline comment; put comments on their own line')
        if not VALUE.fullmatch(value):
            raise InstanceError(f'line {number}: {key} has quotes, spaces, $ or other characters read differently by systemd and bash')
        if key in values:
            raise InstanceError(f'line {number}: {key} is set twice')
        check = FORMATS.get(key)
        if value and check:
            try:
                valid = check(value)
            except ValueError:
                valid = False
            if not valid:
                raise InstanceError(f'line {number}: {key} has an invalid format')
        values[key] = value
    if complete:
        missing = [key for key in REQUIRED if not values.get(key)]
        if missing:
            raise InstanceError('missing values: ' + ', '.join(missing))
    return values


def load(path=DEFAULT, *, complete=False):
    try:
        return parse(Path(path).read_bytes().decode('utf-8'), complete=complete)
    except UnicodeDecodeError:
        raise InstanceError('not UTF-8') from None


def main(argv):
    try:
        if argv[:1] == ['check']:
            rest = argv[1:]
            complete = '--complete' in rest
            rest = [a for a in rest if a != '--complete']
            load(rest[0] if rest else DEFAULT, complete=complete)
            return 0
        if argv[:1] == ['get'] and len(argv) in (2, 3):
            values = load(argv[2] if len(argv) == 3 else DEFAULT)
            if argv[1] not in values:
                raise InstanceError(f'{argv[1]} is not set')
            print(values[argv[1]])
            return 0
        if argv[:1] == ['exec'] and '--' in argv:
            split = argv.index('--')
            command = argv[split + 1:]
            if split > 2 or not command:
                raise SystemExit(__doc__)
            environment = dict(os.environ)
            environment.update(load(argv[1] if split == 2 else DEFAULT))
            os.execvpe(command[0], command, environment)
    except (InstanceError, OSError) as error:
        detail = str(error) if isinstance(error, InstanceError) else type(error).__name__
        print(f'instance.env rejected: {detail}', file=sys.stderr)
        return 2
    raise SystemExit(__doc__)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
