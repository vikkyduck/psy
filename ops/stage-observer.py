"""Stage the existing bot credential for owner-only notices on an independent host."""
import getpass
import importlib.util
import os
from pathlib import Path
import sys


def _common(default="/usr/local/lib/psy-common/psy_common.py"):
    """The shared PSY helpers, loaded by explicit path and never through sys.path: `default`, the
    root-owned installed copy unless a release tool passes the one shipped with it, or in tests
    the copy PSY_COMMON_PATH names."""
    module = sys.modules.get("psy_common")
    if module is None:
        path = str(os.environ.get("PSY_COMMON_PATH") or default)
        spec = importlib.util.spec_from_file_location("psy_common", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module = sys.modules.setdefault("psy_common", module)
    return module


# Run from a release checkout, before anything is installed: use the shared module shipped with it.
psy_common = _common(Path(__file__).resolve().parents[1] / 'common/psy_common.py')


def configuration(text, token):
    values = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith('#'):
            key, separator, value = line.partition('=')
            if not separator or key not in ('PSY_OWNER_ID', 'PSY_PUBLIC_URL', 'TELEGRAM_BOT_TOKEN'):
                raise ValueError('Unexpected observer setting')
            values[key] = value.strip()
    # The rules of the primary host's instance.env; the messages say what the observer needs.
    try:
        owner = psy_common.telegram_id(values.get('PSY_OWNER_ID', ''), 'PSY_OWNER_ID')
    except ValueError:
        raise ValueError('A positive owner ID is required, never a group ID') from None
    try:
        origin = psy_common.https_origin(values.get('PSY_PUBLIC_URL', ''))
    except ValueError:
        raise ValueError('Use the primary HTTPS origin') from None
    try:
        psy_common.bot_token(token)
    except ValueError:
        raise ValueError('Invalid bot credential') from None
    return f'PSY_OWNER_ID={owner}\nPSY_PUBLIC_URL={origin}\nTELEGRAM_BOT_TOKEN={token}\n'


def main():
    if os.geteuid() != 0:
        raise PermissionError('Use root on the independent observer host')
    os.umask(0o077)
    path = Path('/etc/psy-observer/instance.env')
    if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
        raise ValueError('Observer settings must be root-owned and not writable by others')
    result = configuration(path.read_text(), getpass.getpass('Existing Telegram bot token (hidden): '))
    psy_common.atomic_write(path, result.encode())
    print('Owner-only observer credential staged. No message sent and no service started.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit('Observer staging failed: ' + type(error).__name__)
