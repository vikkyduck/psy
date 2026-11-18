"""apply.py's TRACKED and ADDED are the one release inventory; everything else follows them."""
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import apply
import psy_modules

CHECKOUT = Path(psy_modules.__path__[0]).parents[2]
FILES = set(apply.TRACKED + apply.ADDED)


def preflight():
    spec = importlib.util.spec_from_file_location("psy_preflight", apply.HERE.parent/"ops/libexec/preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseInventory(unittest.TestCase):
    def test_committed_manifests_are_generated_from_the_inventory(self):
        manifest = apply.inventory(CHECKOUT)
        self.assertEqual(set(manifest["sha256"]), FILES)
        self.assertEqual(json.loads((apply.HERE/"manifest.json").read_text()), manifest)
        self.assertEqual(json.loads((apply.HERE/"group-manifest.json").read_text()), manifest["sha256"])

    def test_preflight_checks_every_release_file(self):
        check = preflight()
        self.assertEqual(check.EXPECTED, FILES)
        group = json.loads((apply.HERE/"group-manifest.json").read_text())
        with tempfile.TemporaryDirectory() as folder:
            deploy = Path(folder)
            with patch.object(check, "DEPLOY", deploy), patch.object(check, "CHECKOUT", CHECKOUT):
                (deploy/"manifest.json").write_text(json.dumps(group))
                check.verify()
                for broken in ({**group, "tools/transcription_tools.py": "0" * 64},
                               {k: v for k, v in group.items() if k != "tools/transcription_tools.py"}):
                    (deploy/"manifest.json").write_text(json.dumps(broken))
                    with self.assertRaises(ValueError):
                        check.verify()

    def test_readme_counts_follow_the_inventory(self):
        text = (apply.HERE/"README.md").read_text()
        counts = {r"Патч изменяет (\d+) отслеживаемых": len(apply.TRACKED),
                  r"Установщик добавляет (\d+) файл": len(apply.ADDED),
                  r"SHA-256 каждого из (\d+) файл": len(FILES),
                  r"содержит те же (\d+) контрольн": len(FILES)}
        for pattern, expected in counts.items():
            self.assertEqual(int(re.search(pattern, text).group(1)), expected, pattern)


if __name__ == "__main__":
    unittest.main()
