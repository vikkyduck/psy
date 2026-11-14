"""Only generated fixtures are encrypted or inspected, never a working PSY profile."""
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import unittest

from verify_backup import REQUIRED, read_regular_members, inspect_tree, verify


class BackupTests(unittest.TestCase):
    def make_tree(self, root):
        for name in REQUIRED:
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.suffix in ('.db', '.sqlite3'):
                with sqlite3.connect(p) as db:
                    db.execute('CREATE TABLE fixture(value INTEGER)')
                    db.execute('INSERT INTO fixture VALUES (47)')
            else:
                p.write_bytes(b'x' * 32 if name.endswith('recording-signing') else b'synthetic fixture only\n')

    def archive(self, entries):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as output:
            for name, kind, body in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                if kind == tarfile.REGTYPE:
                    member.size = len(body)
                    output.addfile(member, io.BytesIO(body))
                else:
                    member.linkname = '/outside-should-not-exist'
                    output.addfile(member)
        stream.seek(0)
        return stream

    def test_reject_escape_and_absolute_names(self):
        for name in ('../outside', '/outside', 'safe/../../outside', 'safe\\outside'):
            with self.subTest(name=name), TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    read_regular_members(self.archive([(name, tarfile.REGTYPE, b'x')]), Path(tmp))

    def test_links_never_created_and_duplicate_regular_file_refused(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream = self.archive([('link', tarfile.SYMTYPE, b''), ('hard', tarfile.LNKTYPE, b''),
                                   ('safe/file', tarfile.REGTYPE, b'fixture')])
            self.assertEqual(read_regular_members(stream, root), 1)
            self.assertFalse((root/'link').exists())
            self.assertEqual((root/'safe/file').stat().st_mode & 0o777, 0o600)
        with TemporaryDirectory() as tmp:
            with self.assertRaises(FileExistsError):
                read_regular_members(self.archive([('a',tarfile.REGTYPE,b'x')]*2),Path(tmp))

    def test_expansion_bound_and_required_state(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                read_regular_members(self.archive([('large',tarfile.REGTYPE,b'12345')]),root,max_bytes=4)
            with self.assertRaises(ValueError):
                inspect_tree(root)
            self.make_tree(root)
            self.assertEqual(inspect_tree(root),2)
            (root/'db/state.db').write_bytes(b'not a database')
            with self.assertRaises(sqlite3.DatabaseError):
                inspect_tree(root)

    @unittest.skipUnless(shutil.which('gpg') and shutil.which('zstd'), 'gpg and zstd are required')
    def test_real_encrypted_roundtrip_and_wrong_passphrase(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o700)
            source = root/'source'
            source.mkdir()
            self.make_tree(source)
            archive = root/'fixture.tar'
            with tarfile.open(archive,'w') as target:
                for p in sorted(source.rglob('*')):
                    if p.is_file():
                        target.add(p, arcname=str(p.relative_to(source)), recursive=False)
            compressed = root/'fixture.zst'
            subprocess.run(['zstd','-q',str(archive),'-o',str(compressed)],check=True)
            phrase = root/'phrase'
            phrase.write_text('public synthetic test fixture passphrase')
            phrase.chmod(0o600)
            gpg_home=root/'gnupg'
            gpg_home.mkdir(mode=0o700)
            encrypted=root/'fixture.gpg'
            subprocess.run(['gpg','--homedir',str(gpg_home),'--batch','--quiet','--yes',
                            '--pinentry-mode','loopback','--passphrase-file',str(phrase),
                            '--symmetric','--cipher-algo','AES256','--output',str(encrypted),str(compressed)],check=True)
            subprocess.run(['gpgconf','--homedir',str(gpg_home),'--kill','gpg-agent'],check=True)
            stage=root/'verified'
            result=verify(encrypted,phrase,stage=stage)
            self.assertTrue(result['encrypted_archive_verified'])
            self.assertEqual(result['sqlite_snapshots_ok'],2)
            self.assertEqual(result['messages_sent'],0)
            self.assertEqual((stage/'etc/psy-upload/recording-signing').stat().st_size,32)
            self.assertEqual(stage.stat().st_mode&0o777,0o700)
            phrase.write_text('wrong test passphrase')
            with self.assertRaises((ValueError,tarfile.TarError)):
                verify(encrypted,phrase)


if __name__=='__main__':
    unittest.main()
