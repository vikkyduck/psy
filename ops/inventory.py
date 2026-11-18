"""Answer the shell installers' questions about ops/inventory.json, one name per line.

  idle             units that must be inactive before staging or restoring (daemons, sockets, timers)
  alerted          services that get the owner-alert OnFailure= drop-in
  on-failure       the OnFailure= target for those drop-ins
  unit-files NAME  unit files in ops/systemd that installer NAME (runtime, backup, platform) installs
  restart-order    long-running services, in the order they are restarted
"""
import json
from pathlib import Path
import sys

INVENTORY = json.loads((Path(__file__).resolve().parent / 'inventory.json').read_text())
UNITS = INVENTORY['units']


def installed(installer=None):
    return {name: unit for name, unit in UNITS.items()
            if unit['installer'] != 'none' and installer in (None, unit['installer'])}


def query(name, *args):
    if name == 'idle':
        units = installed()
        return ([n for n, u in units.items() if u['kind'] in ('daemon', 'socket')]
                + [u['timer'] for u in units.values() if u.get('timer')])
    if name == 'alerted':
        return [n for n, u in installed().items() if u.get('alert')]
    if name == 'on-failure':
        return [INVENTORY['on_failure']]
    if name == 'unit-files' and len(args) == 1 and args[0] in ('runtime', 'backup', 'platform'):
        units = installed(args[0])
        return list(units) + [u['timer'] for u in units.values() if u.get('timer')]
    if name == 'restart-order':
        return list(INVENTORY['restart_order'])
    raise SystemExit(__doc__)


if __name__ == '__main__':
    print('\n'.join(query(*(sys.argv[1:] or ['']))))
