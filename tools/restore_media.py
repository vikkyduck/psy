"""Decrypt and verify recordings from a private backup, using the media manifest of a verified archive.

Each row of meta/media-manifest.tsv (from `verify_backup.py --stage`) names a SHA-256, a size and the
private path below the uploads root. The matching media/<sha256>.gpg is decrypted into a new file
next to its destination, checked against both, and only then renamed into place with mode 0600.
Only paths below the uploads root are accepted (no "..", no absolute or backslash names), links are
never followed or created, and existing files are never overwritten: an identical one is kept, a
different one is reported. Output is counts only; missing copies are listed by hash, never by path.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile

from verify_backup import relative_name

UPLOADS = PurePosixPath('/home/hermes/.hermes/media-vault/uploads')
CHUNK = 1024**2


def rows(manifest):
    lines = Path(manifest).read_text(encoding='utf-8').splitlines()
    if not lines or lines[0] != 'sha256\tbytes\tpath':
        raise ValueError('Not a PSY media manifest')
    for line in lines[1:]:
        digest, size, path = line.split('\t', 2)
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest) or not size.isdecimal():
            raise ValueError('Malformed manifest row')
        absolute = PurePosixPath(path)
        if not absolute.is_absolute() or UPLOADS not in absolute.parents:
            raise ValueError('Manifest path outside the uploads root')
        yield digest, int(size), relative_name(str(absolute.relative_to(UPLOADS)))


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while block := stream.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def private_directory(root, relative, create):
    """The parent of `relative` below `root`, refusing any link on the way; created (0700) on request.
    None when it does not exist yet and `create` is false."""
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if create:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise ValueError('Link or file where a directory belongs')
        if not current.exists():
            return None
    return current


def decrypt_into(encrypted, directory, size, digest, passphrase, gnupg):
    """Decrypt to a new private file in `directory`; return its path once size and hash match."""
    fd, name = tempfile.mkstemp(prefix='.restoring-', dir=directory)
    temporary = Path(name)
    try:
        written, check = 0, hashlib.sha256()
        with os.fdopen(fd, 'wb') as output, open(os.devnull, 'wb') as quiet:
            gpg = subprocess.Popen(['gpg', '--homedir', str(gnupg), '--batch', '--quiet', '--pinentry-mode', 'loopback',
                                    '--passphrase-file', str(passphrase), '--decrypt', str(encrypted)],
                                   stdout=subprocess.PIPE, stderr=quiet)
            try:
                while block := gpg.stdout.read(CHUNK):
                    written += len(block)
                    if written > size:
                        raise ValueError('Decrypted copy is larger than the manifest says')
                    check.update(block)
                    output.write(block)
            finally:
                gpg.stdout.close()
                if gpg.poll() is None:
                    gpg.kill()
                code = gpg.wait()
        if code or written != size or check.hexdigest() != digest:
            raise ValueError('Decrypted copy does not match the manifest')
        temporary.chmod(0o600)
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def restore(manifest, encrypted_dir, passphrase, target):
    os.umask(0o077)
    target, encrypted_dir = Path(target), Path(encrypted_dir)
    if target.is_symlink() or not target.is_dir():
        raise ValueError('Target must be an existing directory')
    counts = {'restored': 0, 'already_present': 0, 'conflicting': 0, 'missing_encrypted': 0, 'failed': 0}
    missing = []
    with tempfile.TemporaryDirectory(prefix='psy-media-gnupg-') as gnupg:
        os.chmod(gnupg, 0o700)
        try:
            for digest, size, relative in rows(manifest):
                destination = target.joinpath(*relative.parts)
                encrypted = encrypted_dir / (digest + '.gpg')
                try:
                    parent = private_directory(target, relative, create=False)
                    info = os.lstat(destination) if parent is not None else None
                except FileNotFoundError:
                    info = None
                except ValueError:
                    counts['failed'] += 1
                    continue
                if info is not None:
                    same = stat.S_ISREG(info.st_mode) and info.st_size == size and sha256_of(destination) == digest
                    counts['already_present' if same else 'conflicting'] += 1
                    continue
                try:
                    if not stat.S_ISREG(os.lstat(encrypted).st_mode):
                        raise FileNotFoundError
                except FileNotFoundError:
                    counts['missing_encrypted'] += 1
                    missing.append(digest)
                    continue
                try:
                    directory = private_directory(target, relative, create=True)
                    temporary = decrypt_into(encrypted, directory, size, digest, passphrase, gnupg)
                    try:
                        os.link(temporary, destination)   # fails instead of replacing anything
                    finally:
                        temporary.unlink()
                    counts['restored'] += 1
                except (ValueError, OSError):
                    counts['failed'] += 1
        finally:
            subprocess.run(['gpgconf', '--homedir', gnupg, '--kill', 'gpg-agent'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    counts['missing_hashes'] = sorted(set(missing))
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--manifest', type=Path, required=True, help='meta/media-manifest.tsv of a verified archive')
    parser.add_argument('--encrypted', type=Path, required=True, help='directory with the downloaded media/<sha256>.gpg')
    parser.add_argument('--passphrase-file', type=Path, required=True)
    parser.add_argument('--target', type=Path, required=True, help='uploads root to restore into (existing directory)')
    args = parser.parse_args()
    try:
        result = restore(args.manifest, args.encrypted, args.passphrase_file.resolve(strict=True), args.target)
    except (ValueError, OSError) as error:
        raise SystemExit('Media restore failed: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__))
    print(json.dumps(result))
    if result['failed'] or result['conflicting'] or result['missing_encrypted']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
