import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('stage_observer',Path(__file__).resolve().parents[1]/'ops/stage-observer.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ObserverStageTests(unittest.TestCase):
    def test_owner_only_and_no_credential_in_error(self):
        token='123456789:'+('a'*35)
        content='PSY_OWNER_ID=101\nPSY_PUBLIC_URL=https://example.invalid:9443\n'
        result=module.configuration(content,token)
        self.assertIn('PSY_OWNER_ID=101',result)
        for invalid in [content.replace('101','-202'),content.replace('https:','http:'),
                        content.replace('example.invalid','user:secret@example.invalid')]:
            with self.assertRaises(ValueError) as caught:
                module.configuration(invalid,token)
            self.assertNotIn(token,str(caught.exception))

    def test_reject_unknown_config_and_token(self):
        for content in ['PSY_OWNER_ID=101\nUNRECOGNIZED=1', 'PSY_OWNER_ID=0\n']:
            with self.assertRaises(ValueError):module.configuration(content,'invalid')
