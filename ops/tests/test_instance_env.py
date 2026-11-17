import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

OPS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('instance_env', OPS / 'instance_env.py')
env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(env)

GOOD = """# Instance identifiers only.
PSY_OWNER_ID=101
PSY_GROUP_ID=-202
PSY_BOT_ID=303
PSY_BOT_USERNAME=example_bot
PSY_PUBLIC_URL=https://recordings.example.invalid:9443

  # indented comment
PSY_LISTEN_HOST=0.0.0.0
PSY_LISTEN_PORT=9443
PSY_UPLOAD_URL=https://[2001:db8::1]:9443
"""


class InstanceEnv(unittest.TestCase):
    def test_template_and_complete_file(self):
        env.parse((OPS / 'instance.env.example').read_text())
        values = env.parse(GOOD, complete=True)
        self.assertEqual(values['PSY_GROUP_ID'], '-202')
        self.assertEqual(values['PSY_LISTEN_PORT'], '9443')
        with self.assertRaises(env.InstanceError) as missing:
            env.parse((OPS / 'instance.env.example').read_text(), complete=True)
        self.assertIn('PSY_OWNER_ID', str(missing.exception))

    def test_values_read_differently_by_systemd_and_bash_are_rejected(self):
        for line in ('PSY_LISTEN_PORT=9443 # port', 'PSY_BOT_USERNAME="example_bot"', "PSY_BOT_USERNAME='x'",
                     'export PSY_OWNER_ID=101', 'PSY_OWNER_ID = 101', 'PSY_PUBLIC_URL=https://$HOST',
                     'PSY_BOT_USERNAME=a b', 'PSY_BOT_USERNAME=x\\y', 'PSY_BOT_USERNAME=`id`', 'PSY_BOT_USERNAME=x;y',
                     'PSY_OWNER_ID=101\r', 'PSY_OWNER_ID=101\nPSY_OWNER_ID=102', 'lower=1',
                     'PSY_OWNER_ID=-101', 'PSY_GROUP_ID=202', 'PSY_PUBLIC_URL=http://example.invalid',
                     'PSY_PUBLIC_URL=https://example.invalid/path', 'PSY_LISTEN_PORT=70000'):
            with self.subTest(line=line), self.assertRaises(env.InstanceError) as caught:
                env.parse(line + '\n')
            self.assertNotIn('example_bot', str(caught.exception))

    def test_trailing_whitespace_is_not_ambiguous(self):
        self.assertEqual(env.parse('PSY_OWNER_ID=101  \n')['PSY_OWNER_ID'], '101')

    def test_command_line(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'instance.env'
            path.write_text(GOOD)
            run = lambda *args: subprocess.run([sys.executable, '-I', str(OPS / 'instance_env.py'), *args],
                                               capture_output=True, text=True)
            self.assertEqual(run('check', '--complete', str(path)).returncode, 0)
            self.assertEqual(run('get', 'PSY_BOT_ID', str(path)).stdout, '303\n')
            result = run('exec', str(path), '--', sys.executable, '-c', 'import os; print(os.environ["PSY_OWNER_ID"])')
            self.assertEqual(result.stdout, '101\n')
            path.write_text(GOOD + 'PSY_LISTEN_PORT=9443 # again\n')
            result = run('check', str(path))
            self.assertEqual(result.returncode, 2)
            self.assertIn('line 12: PSY_LISTEN_PORT', result.stderr)
            self.assertNotIn('9443', result.stderr)


if __name__ == '__main__':
    unittest.main()
