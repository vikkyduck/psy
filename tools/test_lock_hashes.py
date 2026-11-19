"""Every dependency lock file pins exact versions and carries sha256 hashes for --require-hashes."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
LOCKS = ("media/requirements.lock.txt", "media/asr-requirements.lock.txt",
         "gateway/integration/requirements.lock.txt")
PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9.+!_-]+$")
HASH = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")


def requirements(path):
    """(pin, [hashes]) per requirement, joining backslash continuations."""
    text = re.sub(r"\\\n", " ", path.read_text())
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pin, *options = line.split()
        yield pin, options


class LockFiles(unittest.TestCase):
    def test_every_requirement_is_pinned_and_hashed(self):
        for name in LOCKS:
            with self.subTest(lock=name):
                found = list(requirements(ROOT / name))
                self.assertTrue(found)
                for pin, options in found:
                    self.assertRegex(pin, PIN)
                    self.assertTrue(options, f"{pin} has no hashes")
                    for option in options:
                        self.assertRegex(option, HASH)

    def test_installs_require_hashes(self):
        workflow = (ROOT / ".github/workflows/restore.yml").read_text()
        docs = (ROOT / "ops/README.md").read_text() + (ROOT / "media/README.md").read_text()
        for text in (workflow, docs):
            for line in text.splitlines():
                if "pip" in line and " install " in line and "lock.txt" in line:
                    self.assertIn("--require-hashes", line)


if __name__ == "__main__":
    unittest.main()
