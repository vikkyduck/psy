"""prepare-host and restore-permissions share ops/acl-policy.sh: a recovered tree must end up
with the same effective rights as one the services created under the default ACLs."""
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import unittest

OPS = Path(__file__).resolve().parents[1]
USERS = ('psy-upload', 'psy-deliver', 'psy-asr')


def accounts_exist():
    try:
        for user in USERS:
            pwd.getpwnam(user)
        return True
    except KeyError:
        return False


def policy(function, directory, name):
    subprocess.run(['bash', '-ec', '. "$1"; "$2" "$3" "$4"', 'bash', str(OPS / 'acl-policy.sh'),
                    function, str(directory), name], check=True)


def effective(path):
    """{('user', name) or ('default', name): effective rights} for the service accounts."""
    out = subprocess.run(['getfacl', '-cpe', str(path)], capture_output=True, text=True, check=True).stdout
    rights = {}
    for line in out.splitlines():
        entry, _, comment = line.partition('#')
        parts = entry.strip().split(':')
        if len(parts) == 4 and parts[0] == 'default' and parts[1] == 'user' and parts[2] in USERS:
            rights[('default', parts[2])] = (comment.split(':')[-1] or parts[3]).strip()
        elif len(parts) == 3 and parts[0] == 'user' and parts[1] in USERS:
            rights[('user', parts[1])] = (comment.split(':')[-1] if comment else parts[2]).strip()
    return rights


@unittest.skipUnless(shutil.which('setfacl') and shutil.which('getfacl') and accounts_exist(),
                     'needs POSIX ACL tools and the PSY service accounts')
class SharedAcl(unittest.TestCase):
    def test_restored_tree_matches_a_fresh_one(self):
        for name in ('MEDIA', 'QUEUE', 'PROGRESS'):
            with self.subTest(policy=name), tempfile.TemporaryDirectory() as folder:
                fresh, restored = Path(folder, 'fresh'), Path(folder, 'restored')
                for tree in (fresh, restored):
                    tree.mkdir(mode=0o700)
                # The services create entries under the default ACL...
                policy('psy_shared_acl', fresh, name)
                os.close(os.open(fresh / 'recording', os.O_CREAT | os.O_WRONLY, 0o666))
                (fresh / 'job').mkdir()
                # ...while a restore brings plain private files back and rebuilds the ACLs.
                os.close(os.open(restored / 'recording', os.O_CREAT | os.O_WRONLY, 0o600))
                (restored / 'job').mkdir(mode=0o700)
                policy('psy_shared_acl_entries', restored, name)
                policy('psy_shared_acl', restored, name)
                for relative in ('.', 'recording', 'job'):
                    self.assertEqual(effective(fresh / relative), effective(restored / relative), relative)
                    self.assertTrue(effective(fresh / relative), relative)

    def test_media_rights(self):
        with tempfile.TemporaryDirectory() as folder:
            media = Path(folder, 'media')
            media.mkdir(mode=0o700)
            policy('psy_shared_acl', media, 'MEDIA')
            rights = effective(media)
            # The sender writes results next to recordings; ASR may only traverse, never list.
            self.assertEqual(rights[('user', 'psy-deliver')], 'rwx')
            self.assertEqual(rights[('user', 'psy-asr')], '--x')
            self.assertEqual(rights[('default', 'psy-asr')], 'r-x')


if __name__ == '__main__':
    unittest.main()
