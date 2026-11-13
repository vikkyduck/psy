import os
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import psy_upload_common as common


class InstanceTests(unittest.TestCase):
    def test_incomplete_or_invalid_config_stops_import(self):
        for key, bad in [('PSY_OWNER_ID',None), ('PSY_GROUP_ID','0'), ('PSY_BOT_ID','-303'),
                         ('PSY_BOT_USERNAME','@fixture_bot'), ('PSY_PUBLIC_URL','http://fixture.invalid'),
                         ('PSY_PUBLIC_URL','https://user@fixture.invalid')]:
            with self.subTest(key=key,value=bad):
                env = os.environ.copy()
                env.pop(key)
                if bad is not None:
                    env[key] = bad
                run = subprocess.run([sys.executable, '-c', 'import psy_upload_common'], env=env,
                                     cwd=Path(common.__file__).parent, capture_output=True)
                self.assertNotEqual(run.returncode, 0)

    def test_token_from_other_bot_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, CREDENTIALS_DIRECTORY=tmp):
            (Path(tmp)/'telegram').write_text(str(common.BOT_ID+1)+':'+'x'*35)
            with self.assertRaises(RuntimeError):
                common.bot_settings()
