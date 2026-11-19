"""The one reader of /etc/psy/instance.env for ops scripts.

systemd (EnvironmentFile=), bash (`. file`) and python-dotenv disagree on quotes, inline comments,
spaces and $: `PSY_LISTEN_PORT=9443 # port` is "9443" to bash and dotenv but "9443 # port" to
systemd. Only lines all three read the same way are accepted: comment lines starting with #,
blank lines and KEY=value with a plain value. Errors name the line and key, never the value.
The values must have the format every PSY service requires; with --complete the five identity
values must be set and the bot and owner IDs must differ.

  instance_env.py check [--complete] [FILE]     validate (and with --complete require the IDs)
  instance_env.py get KEY [FILE]                print one value
  instance_env.py exec [FILE] -- COMMAND...     run COMMAND with the values in its environment
"""
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


# Run from a release checkout, also before anything is installed: use the shared module shipped
# with it. The rules are those every service applies to the same values (psy_common.instance).
psy_common = _common(Path(__file__).resolve().parents[1] / 'common/psy_common.py')
DEFAULT = psy_common.INSTANCE_FILE
InstanceError = psy_common.InstanceError
parse = psy_common.parse_instance
load = psy_common.load_instance


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
