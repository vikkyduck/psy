"""Offline group boundary and Codex adapter tests using only synthetic fixtures."""
import argparse
import os
from pathlib import Path
import sys
import types
import unittest

from apply import HERE, TELEGRAM, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    checkout = parser.parse_args().checkout.resolve()
    run(checkout, apply=False)
    os.environ.update(PSY_OWNER_ID="101", PSY_GROUP_ID="-202", PSY_BOT_ID="303",
                      PSY_BOT_USERNAME="DemoTestBot", PSY_PUBLIC_URL="https://example.invalid",
                      # Installed code loads /usr/local/lib/psy-common/psy_common.py; tests use this copy.
                      PSY_COMMON_PATH=str(HERE.parent/"common/psy_common.py"))
    # Load the actual restored modules without starting the real Telegram adapter.
    sys.path.insert(0, str(checkout))
    pkg = types.ModuleType("psy_modules")
    pkg.__path__ = [str(checkout/TELEGRAM)]
    sys.modules[pkg.__name__] = pkg
    tests = unittest.defaultTestLoader.discover(str(HERE/"tests"))
    return 0 if unittest.TextTestRunner(verbosity=1).run(tests).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
