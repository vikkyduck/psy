"""The installed upload handoff loads the uploader's module by path and leaves sys.path alone."""
import importlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import psy_modules


def load_handoff(common):
    telegram = ModuleType("telegram")
    telegram.InlineKeyboardButton = telegram.InlineKeyboardMarkup = telegram.WebAppInfo = dict
    spec = importlib.util.spec_from_file_location(
        "fixture_upload_handoff", Path(psy_modules.__path__[0]) / "psy_upload_handoff.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"telegram": telegram, "psy_upload_common": common}):
        spec.loader.exec_module(module)
    return module


class HandoffImport(unittest.TestCase):
    def test_loaded_common_module_is_reused(self):
        common = ModuleType("psy_upload_common")
        path = list(sys.path)
        handoff = load_handoff(common)
        self.assertIs(handoff.C, common)
        self.assertEqual(sys.path, path)

    def test_uploader_directory_is_used_only_while_loading_and_never_shadows(self):
        handoff = load_handoff(ModuleType("psy_upload_common"))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).resolve()
            (folder / "psy_upload_common.py").write_text(
                "import json\nimport fixture_grant\nVALUE = fixture_grant.VALUE\nSTDLIB_JSON = json.dumps is not None\n")
            (folder / "fixture_grant.py").write_text("VALUE = 'loaded by path'\n")
            (folder / "json.py").write_text("raise RuntimeError('uploader file shadowed the standard library')\n")
            path, finders = list(sys.path), list(sys.meta_path)
            with patch.dict(sys.modules):
                sys.modules.pop("psy_upload_common", None)
                common = handoff.load(folder)
                self.assertEqual((common.VALUE, common.STDLIB_JSON), ("loaded by path", True))
                self.assertEqual(Path(sys.modules["fixture_grant"].__file__).resolve().parent, folder)
                self.assertIs(sys.modules["json"], json)
            self.assertEqual((sys.path, sys.meta_path), (path, finders))
            self.assertNotIn("psy_upload_common", sys.modules)
            with self.assertRaises(ImportError):
                importlib.import_module("fixture_grant")

    def test_failed_load_leaves_no_partial_module(self):
        handoff = load_handoff(ModuleType("psy_upload_common"))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "psy_upload_common.py").write_text("import fixture_missing_dependency\n")
            finders = list(sys.meta_path)
            with patch.dict(sys.modules):
                sys.modules.pop("psy_upload_common", None)
                with self.assertRaises(ImportError):
                    handoff.load(Path(tmp))
                self.assertNotIn("psy_upload_common", sys.modules)
            self.assertEqual(sys.meta_path, finders)


if __name__ == "__main__":
    unittest.main()
