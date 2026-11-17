"""Media restore on generated recordings only."""
import hashlib
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

from restore_media import UPLOADS, restore, rows


def encrypt(source, target, phrase, home):
    subprocess.run(['gpg', '--homedir', str(home), '--batch', '--quiet', '--yes', '--pinentry-mode', 'loopback',
                    '--passphrase-file', str(phrase), '--symmetric', '--cipher-algo', 'AES256',
                    '--compress-algo', 'none', '--output', str(target), str(source)], check=True)


@unittest.skipUnless(shutil.which('gpg'), 'gpg is required')
class MediaRestore(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        root = self.root = Path(self.folder.name)
        root.chmod(0o700)
        self.phrase = root / 'phrase'
        self.phrase.write_text('public synthetic test fixture passphrase')
        self.home = root / 'gnupg'
        self.home.mkdir(mode=0o700)
        self.encrypted = root / 'media'
        self.encrypted.mkdir()
        self.target = root / 'uploads'
        self.target.mkdir(mode=0o700)
        self.recordings = {'lecture.ogg': b'synthetic audio ' * 5000, 'job/lecture.txt': b'synthetic transcript\n'}
        lines = ['sha256\tbytes\tpath']
        for name, body in self.recordings.items():
            digest = hashlib.sha256(body).hexdigest()
            plain = root / ('plain-' + digest)
            plain.write_bytes(body)
            encrypt(plain, self.encrypted / (digest + '.gpg'), self.phrase, self.home)
            lines.append(f'{digest}\t{len(body)}\t{UPLOADS}/{name}')
        self.manifest = root / 'media-manifest.tsv'
        self.manifest.write_text('\n'.join(lines) + '\n')

    def tearDown(self):
        subprocess.run(['gpgconf', '--homedir', str(self.home), '--kill', 'gpg-agent'], check=False)
        self.folder.cleanup()

    def run_restore(self):
        return restore(self.manifest, self.encrypted, self.phrase, self.target)

    def test_restore_verify_and_never_overwrite(self):
        result = self.run_restore()
        self.assertEqual((result['restored'], result['failed'], result['missing_encrypted']), (2, 0, 0))
        for name, body in self.recordings.items():
            self.assertEqual((self.target / name).read_bytes(), body)
            self.assertEqual((self.target / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_restore()['already_present'], 2)
        (self.target / 'lecture.ogg').write_bytes(b'something else')
        self.assertEqual(self.run_restore()['conflicting'], 1)
        self.assertEqual((self.target / 'lecture.ogg').read_bytes(), b'something else')
        self.assertEqual([p.name for p in self.target.rglob('.restoring-*')], [])

    def test_wrong_content_missing_copy_and_links(self):
        digest = self.manifest.read_text().splitlines()[1].split('\t')[0]
        other = self.root / 'other'
        other.write_bytes(b'not the recording')
        encrypt(other, self.encrypted / (digest + '.gpg'), self.phrase, self.home)
        (self.target / 'job').symlink_to(self.root)
        result = self.run_restore()
        self.assertEqual(result['failed'], 2)   # wrong bytes; a link where the folder belongs
        self.assertFalse((self.target / 'lecture.ogg').exists())
        self.assertFalse((self.root / 'lecture.txt').exists())
        (self.target / 'job').unlink()
        (self.encrypted / (self.manifest.read_text().splitlines()[2].split('\t')[0] + '.gpg')).unlink()
        result = self.run_restore()
        self.assertEqual(result['missing_encrypted'], 1)
        self.assertEqual(len(result['missing_hashes']), 1)

    def test_manifest_paths_must_stay_below_the_uploads_root(self):
        for path in ('/etc/passwd', f'{UPLOADS}/../escape', f'{UPLOADS}/a\\b', 'relative/name'):
            self.manifest.write_text(f'sha256\tbytes\tpath\n{"0" * 64}\t1\t{path}\n')
            with self.subTest(path=path), self.assertRaises(ValueError):
                list(rows(self.manifest))


if __name__ == '__main__':
    unittest.main()
