"""Run with the upload Python environment, using only loopback and disposable data."""
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

MEDIA = Path(__file__).resolve().parent.parent

def main():
    with TemporaryDirectory(prefix='psy-fixtures-') as temp:
        os.environ.update(
            PSY_OWNER_ID='101', PSY_GROUP_ID='-202', PSY_BOT_ID='303',
            PSY_BOT_USERNAME='fixture_bot', PSY_PUBLIC_URL='https://fixture.invalid',
            HERMES_HOME=temp, PSY_UPLOAD_ROOT=temp+'/state', PSY_UPLOAD_FILES=temp+'/files',
            PSY_ASR_PROGRESS=temp+'/progress', PSY_ACCESS_SOCKET=temp+'/member.sock',
            PSY_MEMBER_QUOTA_BYTES=str(4*1024**3),
        )
        os.environ.pop('CREDENTIALS_DIRECTORY', None)
        sys.path.insert(0, str(MEDIA))
        sys.path.insert(0, str(MEDIA/'tests'))
        suite = unittest.defaultTestLoader.discover(str(MEDIA/'tests'))
        if not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful():
            return 1
        subprocess.run(['node','--test', str(MEDIA/'tests/test_transfer.cjs')],check=True)
        import asyncio
        from test_transfer_http import main as transfer_http
        asyncio.run(transfer_http())
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
