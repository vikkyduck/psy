"""Operator view of group jobs that wait for a decision. Run it as the hermes user:

    sudo -u hermes python3 -I PATH/psy_group_admin.py list [--all]
    sudo -u hermes python3 -I PATH/psy_group_admin.py resolve ID delivered|requeue|drop

PATH is plugins/platforms/telegram in the Hermes checkout. The gateway may keep running:
it never runs a job in these states, and a requeued job starts within its 30 s check.
Only identifiers and job metadata are printed, never message text or answers."""
import argparse
import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from . import psy_group_store as store
else:
    # Run by path: load the neighbouring store explicitly, without changing sys.path.
    _spec = importlib.util.spec_from_file_location(
        "psy_group_store", Path(__file__).resolve().with_name("psy_group_store.py"))
    store = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(store)

ACTIONS = ("delivered", "requeue", "drop")
STATES = {
    "uncertain": "a part may or may not have reached the group",
    "failed": "an unexpected error was reported to the member",
    "queued": "waiting",
    "running": "running",
    "sending": "sending",
}


def _when(stamp):
    if stamp is None:
        return "-"
    if stamp <= 0:
        return "now"
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def describe(row):
    (ident, state, uid, topic, kept, count, size, answer, sent, pending,
     attempts, retry_at, kind, updated, stage) = row
    retry = f"retry {attempts} ({kind or 'telegram'}) due {_when(retry_at)}" if attempts else "no retry timer"
    return (f"{ident}  {state}: {STATES.get(state, state)}\n"
            f"    member {uid}, topic {topic}; request text {'kept' if kept else 'not kept'}; "
            f"{count} checkpoints ({(size + 1023) // 1024} KiB){', answer saved' if answer else ''}\n"
            f"    parts sent {sent}, unconfirmed {pending}; {retry}; last {stage or '-'} at {_when(updated)}")


def main(argv=None, *, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=store.ROOT, help="group vault directory")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="jobs that are uncertain, failed or waiting for a retry")
    listing.add_argument("--all", action="store_true", help="also list running and fresh queued jobs")
    settle = commands.add_parser("resolve", help="settle one job")
    settle.add_argument("ident", type=int)
    settle.add_argument("action", choices=ACTIONS)
    opts = parser.parse_args(argv)
    if os.geteuid() == 0:
        parser.error("run as the hermes user, for example with sudo -u hermes")
    if not (opts.root / "group.sqlite3").is_file():
        parser.error(f"no group vault in {opts.root}")
    if opts.command == "list":
        rows = store.jobs(root=opts.root, include_all=opts.all)
        for row in rows:
            print(describe(row), file=out)
        if not rows:
            print("No group jobs need a decision.", file=out)
        return 0
    try:
        store.resolve(opts.ident, opts.action, root=opts.root)
    except (LookupError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(f"{opts.ident}: {opts.action}", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
