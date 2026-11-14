"""Stage the existing bot credential for owner-only notices on an independent host."""
import getpass
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


def configuration(text, token):
    values = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith('#'):
            key, separator, value = line.partition('=')
            if not separator or key not in ('PSY_OWNER_ID', 'PSY_PUBLIC_URL', 'TELEGRAM_BOT_TOKEN'):
                raise ValueError('Unexpected observer setting')
            values[key] = value.strip()
    owner = values.get('PSY_OWNER_ID', '')
    origin = values.get('PSY_PUBLIC_URL', '').rstrip('/')
    parsed = urlsplit(origin)
    if not owner.isdecimal() or not 0 < int(owner) < 2**52:
        raise ValueError('A positive owner ID is required, never a group ID')
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
            or re.search(r'[\s\\#]', origin)):
        raise ValueError('Use the primary HTTPS origin')
    _ = parsed.port
    if not re.fullmatch(r'[1-9][0-9]{5,14}:[A-Za-z0-9_-]{30,}', token):
        raise ValueError('Invalid bot credential')
    return f'PSY_OWNER_ID={owner}\nPSY_PUBLIC_URL={origin}\nTELEGRAM_BOT_TOKEN={token}\n'


def main():
    if os.geteuid() != 0:
        raise PermissionError('Use root on the independent observer host')
    os.umask(0o077)
    path = Path('/etc/psy-observer/instance.env')
    if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
        raise ValueError('Observer settings must be root-owned and not writable by others')
    result = configuration(path.read_text(), getpass.getpass('Existing Telegram bot token (hidden): '))
    fd, name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(result)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    print('Owner-only observer credential staged. No message sent and no service started.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit('Observer staging failed: ' + type(error).__name__)
