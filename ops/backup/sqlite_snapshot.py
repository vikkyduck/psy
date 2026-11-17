#!/usr/bin/python3
"""Consistent SQLite snapshots for psy-backup, made by the database owner and never as root.

Usage: sqlite_snapshot.py ROOT OUT DATABASE...
Each DATABASE (below ROOT) is copied to OUT/<path relative to ROOT> and OUT/db-notes.txt gets one
"<path>: <quick_check result>" line per database. psy-backup fails the run on any line other than "ok".

The copy goes a few pages at a time with a short pause between steps, so the bot never waits long for
its database. A WAL database is read inside one read transaction: the copy sees a single snapshot,
never restarts and never blocks a writer. The copy is converted to a self-contained rollback-journal
file, so the archive needs no -wal or -shm next to it.
"""
import pathlib
import shutil
import sqlite3
import sys
import time

PAGES = 256      # pages per step (1 MiB with 4 KiB pages)
PAUSE = 0.02     # seconds between steps
RESTARTS = 3     # restarts of a busy rollback-journal copy before one single-step copy


class Restarted(Exception):
    """A rollback-journal database kept changing under a paced copy."""


def journal_mode(connection):
    return str(connection.execute('PRAGMA journal_mode').fetchone()[0]).lower()


def remove_sidecars(path, *, including_database=False):
    for suffix in ('-wal', '-shm', '-journal') + (('',) if including_database else ()):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def snapshot(source_path, target_path, *, pages=PAGES, pause=PAUSE):
    """Copy one database and return its quick_check result."""
    source_path, target_path = pathlib.Path(source_path), pathlib.Path(target_path)
    remove_sidecars(target_path, including_database=True)
    source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True, timeout=30, isolation_level=None)
    try:
        if journal_mode(source) == 'wal':
            # Pin one WAL snapshot for the whole copy. Writers keep appending to the WAL meanwhile.
            source.execute('BEGIN')
            source.execute('SELECT count(*) FROM sqlite_master').fetchone()
        target = sqlite3.connect(target_path, isolation_level=None)
        try:
            progress = {'restarts': 0, 'remaining': None}

            def pace(status, remaining, total):
                # Outside a pinned snapshot a commit by another process restarts the copy from page 1:
                # a completed step then leaves no fewer pages to go than the step before.
                if status == sqlite3.SQLITE_OK and progress['remaining'] is not None and remaining >= progress['remaining']:
                    progress['restarts'] += 1
                    if progress['restarts'] > RESTARTS:
                        raise Restarted()
                progress['remaining'] = remaining
                if remaining:
                    time.sleep(pause)

            try:
                source.backup(target, pages=pages, progress=pace)
            except Restarted:
                # Constant writes to a rollback-journal database: one short single-step copy instead.
                source.backup(target)
            if source.in_transaction:
                source.execute('COMMIT')
            if journal_mode(target) != 'delete':
                target.execute('PRAGMA journal_mode=DELETE')
            return target.execute('PRAGMA quick_check').fetchone()[0]
        finally:
            target.close()
            # The copy is in rollback-journal mode now; an index file left by its WAL phase is stale.
            remove_sidecars(target_path)
    finally:
        source.close()


def plain_copy(source_path, target_path):
    """Keep whatever bytes exist when a database cannot be opened. The run still fails."""
    shutil.copy2(source_path, target_path)
    wal = source_path.with_name(source_path.name + '-wal')
    if wal.is_file():
        shutil.copy2(wal, target_path.with_name(target_path.name + '-wal'))


def main(argv):
    root, out = pathlib.Path(argv[1]), pathlib.Path(argv[2])
    notes = []
    for source in map(pathlib.Path, argv[3:]):
        target = out / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            notes.append(f'{source}: {snapshot(source, target)}')
        except Exception as exc:  # not SQLite, or unreadable: keep a plain copy so nothing is lost
            plain_copy(source, target)
            notes.append(f'{source}: plain copy ({type(exc).__name__}: {exc})')
    (out / 'db-notes.txt').write_text(''.join(line + '\n' for line in notes))


if __name__ == '__main__':
    main(sys.argv)
