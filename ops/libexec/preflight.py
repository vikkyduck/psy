"""Refuse to run the owner gateway with the audited boundary missing after an upgrade."""
import hashlib
import json
import sys
from pathlib import Path

DEPLOY = Path("/usr/local/lib/psy-group")
PLATFORM = Path("/home/hermes/.hermes/hermes-agent/plugins/platforms/telegram")
EXPECTED = {
    "adapter.py", "update_admission.py", "psy_instance.py", "psy_group_route.py",
    "psy_group_store.py", "psy_group_codex.py", "psy_group_audio.py",
    "psy_group_queue.py", "psy_local_media.py", "psy_upload_handoff.py",
}

def verify():
    manifest = json.loads((DEPLOY/"manifest.json").read_text())
    if set(manifest) != EXPECTED:
        raise ValueError()
    for name in EXPECTED:
        if hashlib.sha256((PLATFORM/name).read_bytes()).hexdigest() != manifest[name]:
            raise ValueError()
    source = (PLATFORM/"adapter.py").read_text()
    flow = ('builder.application_class(TelegramApplication, {"adapter": self})',
            'from plugins.platforms.telegram.update_admission import TelegramApplication, build_update_processor')
    if not all(item in source for item in flow):
        raise ValueError()

if __name__ == "__main__":
    try:
        verify()
    except Exception:
        sys.exit("PSY group routing integrity check failed. Review group boundary before starting Telegram.")
