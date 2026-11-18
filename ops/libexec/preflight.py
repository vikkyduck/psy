"""Refuse to run the owner gateway with the audited boundary missing after an upgrade."""
import hashlib
import json
import sys
from pathlib import Path

DEPLOY = Path("/usr/local/lib/psy-group")
CHECKOUT = Path("/home/hermes/.hermes/hermes-agent")
PLATFORM = "plugins/platforms/telegram/"
# The release inventory of gateway/apply.py (TRACKED + ADDED). A gateway test keeps them equal.
EXPECTED = {
    "agent/auxiliary_client.py", "agent/credential_pool.py", "hermes_cli/auth.py", "hermes_cli/auth_codex.py",
    "hermes_cli/psy_codex_incident.py", "hermes_cli/psy_codex_policy.py", "tools/transcription_tools.py",
    *(PLATFORM + name for name in (
        "adapter.py", "update_admission.py", "psy_instance.py", "psy_group_route.py", "psy_source_evidence.py",
        "psy_group_store.py", "psy_group_codex.py", "psy_group_audio.py", "psy_group_queue.py",
        "psy_owner_recovery.py", "psy_upload_handoff.py", "psy_group_admin.py", "psy_group_prompts.py",
        "psy_group_client.py", "psy_group_pipeline.py", "psy_group_sources.py", "psy_group_lecture.py",
        "psy_group_continuation.py")),
}
# gateway/apply.py SHARED: root-owned code outside the checkout that the gateway loads by explicit
# path. Manifest key -> the file checked.
SHARED = {"/usr/local/lib/psy-common/psy_common.py": Path("/usr/local/lib/psy-common/psy_common.py")}

def verify():
    manifest = json.loads((DEPLOY/"manifest.json").read_text())
    if set(manifest) != EXPECTED | set(SHARED):
        raise ValueError()
    for name in EXPECTED:
        if hashlib.sha256((CHECKOUT/name).read_bytes()).hexdigest() != manifest[name]:
            raise ValueError()
    for name, path in SHARED.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest[name]:
            raise ValueError()
    source = (CHECKOUT/PLATFORM/"adapter.py").read_text()
    flow = ('builder.application_class(TelegramApplication, {"adapter": self})',
            'from plugins.platforms.telegram.update_admission import TelegramApplication, build_update_processor')
    if not all(item in source for item in flow):
        raise ValueError()

if __name__ == "__main__":
    try:
        verify()
    except Exception:
        sys.exit("PSY group routing integrity check failed. Review group boundary before starting Telegram.")
