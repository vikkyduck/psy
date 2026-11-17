"""Verify a private encrypted backup in disposable storage, without starting any PSY service."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile

# Also the nightly "must be in the archive" list: ops/backup/psy-backup runs this verifier on every copy.
REQUIRED = (
    'home/hermes/.hermes/config.yaml', 'home/hermes/.hermes/SOUL.md',
    'home/hermes/.hermes/.env', 'home/hermes/.hermes/auth.json',
    'etc/psy/instance.env', 'etc/psy-upload/recording-signing', 'db/state.db',
    'sysdb/var/lib/psy-recording-receipts/results.sqlite3', 'meta/media-manifest.tsv',
)
MAX_BYTES = 10 * 1024**3
MAX_FILES = 100000


class MissingState(ValueError):
    """Names only the fixed public paths from REQUIRED, never archive content."""


def relative_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or not path.parts:
        raise ValueError('Unsafe archive member')
    return path


def read_regular_members(stream, destination, *, max_bytes=MAX_BYTES):
    """Inspect names but never follow links or restore device nodes, modes or ownership."""
    total = count = 0
    with tarfile.open(fileobj=stream, mode='r|') as archive:
        for member in archive:
            path = relative_name(member.name)
            if not member.isfile():
                continue
            count += 1
            total += member.size
            if count > MAX_FILES or member.size < 0 or total > max_bytes:
                raise ValueError('Backup exceeds verification bounds')
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with archive.extractfile(member) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output, 1024**2)
            target.chmod(0o600)
    return count


def inspect_tree(root, *, tolerate_sqlite_errors=False):
    """Return (good SQLite snapshots, failed ones). Failures raise unless tolerated."""
    missing = [name for name in REQUIRED if not (root / name).is_file()]
    if missing:
        raise MissingState('Required recovery state is missing: ' + ', '.join(missing))
    count = failed = 0
    for base in ('db', 'sysdb'):
        for path in (root / base).rglob('*'):
            if path.is_file() and path.suffix in ('.db', '.sqlite', '.sqlite3'):
                try:
                    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as database:
                        if database.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                            raise ValueError('Invalid SQLite snapshot')
                except (ValueError, sqlite3.Error):
                    if not tolerate_sqlite_errors:
                        raise
                    failed += 1
                    continue
                count += 1
    if (root / 'etc/psy-upload/recording-signing').stat().st_size != 32:
        raise ValueError('Invalid recording signing state')
    return count, failed


def retain_verified_tree(source, destination):
    destination = Path(destination)
    parent = destination.parent.resolve(strict=True)
    if destination.exists() or destination.is_symlink() or parent.stat().st_mode & 0o077:
        raise ValueError('Use an absent staging directory below a private parent')
    temporary = Path(tempfile.mkdtemp(prefix='.verified-', dir=parent))
    try:
        shutil.copytree(source, temporary, dirs_exist_ok=True)
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def verify(archive, passphrase, *, scratch_parent=None, stage=None, tolerate_sqlite_errors=False):
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='psy-backup-check-', dir=scratch_parent) as directory:
        root = Path(directory)
        restored = root / 'regular'
        restored.mkdir(mode=0o700)
        private_home = root / 'gnupg'
        private_home.mkdir(mode=0o700)
        with (root / 'private-errors').open('wb') as errors:
            gpg = subprocess.Popen(['gpg', '--homedir', str(private_home), '--batch', '--quiet',
                                    '--pinentry-mode', 'loopback', '--passphrase-file', str(passphrase),
                                    '--decrypt', str(archive)], stdout=subprocess.PIPE, stderr=errors)
            zstd = subprocess.Popen(['zstd', '-dcq'], stdin=gpg.stdout, stdout=subprocess.PIPE, stderr=errors)
            gpg.stdout.close()
            try:
                files = read_regular_members(zstd.stdout, restored)
                while zstd.stdout.read(1024**2):
                    pass
                if zstd.wait(timeout=60) or gpg.wait(timeout=60):
                    raise ValueError('Decryption or decompression failed')
                databases, failed = inspect_tree(restored, tolerate_sqlite_errors=tolerate_sqlite_errors)
                if stage is not None:
                    retain_verified_tree(restored, stage)
            finally:
                zstd.stdout.close()
                for process in (zstd, gpg):
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                subprocess.run(['gpgconf', '--homedir', str(private_home), '--kill', 'gpg-agent'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return {'encrypted_archive_verified': True, 'regular_files': files,
            'sqlite_snapshots_ok': databases, 'sqlite_snapshots_failed': failed,
            'staged_regular_files': stage is not None,
            'services_started': 0, 'messages_sent': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--passphrase-file', type=Path, required=True)
    parser.add_argument('--scratch-parent', type=Path)
    parser.add_argument('--stage', type=Path, help='Keep verified regular files in a new private directory')
    parser.add_argument('--tolerate-sqlite-errors', action='store_true',
                        help='Count damaged SQLite snapshots instead of failing (the nightly run reports them itself)')
    args = parser.parse_args()
    try:
        result = verify(args.archive.resolve(strict=True), args.passphrase_file.resolve(strict=True),
                        scratch_parent=args.scratch_parent, stage=args.stage,
                        tolerate_sqlite_errors=args.tolerate_sqlite_errors)
    except MissingState as error:
        raise SystemExit('Backup verification failed: ' + str(error))
    except Exception as error:
        raise SystemExit('Backup verification failed: ' + type(error).__name__)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
