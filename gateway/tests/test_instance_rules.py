"""The gateway's psy_instance and psy_common.instance() (upload side and ops) decide alike."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import psy_modules

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("psy_common_rules", REPO / "common/psy_common.py")
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)
SOURCE = Path(psy_modules.__path__[0]) / "psy_instance.py"
BASE = {"PSY_OWNER_ID": "101", "PSY_GROUP_ID": "-202", "PSY_BOT_ID": "303",
        "PSY_BOT_USERNAME": "DemoTestBot", "PSY_PUBLIC_URL": "https://example.invalid"}
VARIANTS = {
    "PSY_OWNER_ID": ("0101", "+101", "-101", "١٠١", str(2**52), str(2**52 - 1), "303", " 101", ""),
    "PSY_GROUP_ID": ("202", "-0", "-0202", "-1001234567890", "-" + str(2**52), "- 202", ""),
    "PSY_BOT_ID": ("101", "0", "1" * 17, "12345678901", ""),
    "PSY_BOT_USERNAME": ("abot", "a_bot", "@DemoTestBot", "Demo-Bot", "x" * 29 + "BOT", "x" * 30 + "bot", "demo", ""),
    "PSY_PUBLIC_URL": ("https://example.invalid/", "https://example.invalid//", "HTTPS://example.invalid",
                       "https://example.invalid:0", "https://example.invalid:9443", "https://example.invalid:65536",
                       "https://example.invalid:x", "https://example.invalid?", "https://example.invalid#",
                       "https://u@example.invalid", "https://example.invalid/path", "https://пример.invalid",
                       "https://example.invalid\x01", "https://example.invalid\\", "https://[2001:db8::1]:9443",
                       "https://[::1", "http://example.invalid", ""),
}


def gateway(env):
    spec = importlib.util.spec_from_file_location("psy_instance_rules_probe", SOURCE)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, env):
        try:
            spec.loader.exec_module(module)
        except ValueError:
            return None
    return module.OWNER, module.GROUP, module.BOT, module.BOT_USERNAME, module.PUBLIC_URL


def shared(env):
    try:
        return tuple(common.instance(env))
    except ValueError:
        return None


class InstanceRules(unittest.TestCase):
    def test_gateway_and_shared_rules_agree(self):
        self.assertEqual(gateway(BASE), (101, -202, 303, "DemoTestBot", "https://example.invalid"))
        for key, values in VARIANTS.items():
            for value in values:
                env = {**BASE, key: value}
                with self.subTest(key=key, value=value):
                    self.assertEqual(gateway(env), shared(env))


if __name__ == "__main__":
    unittest.main()
