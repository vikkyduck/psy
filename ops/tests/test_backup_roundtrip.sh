#!/bin/bash
# Fresh CI host only (after prepare-host and install-runtime): run the installed nightly backup in
# its own unit sandbox into a local directory, twice (the second time as on Sundays), verify the
# newest archive with the restore verifier and restore the recordings with tools/restore_media.py.
# Only synthetic data is created; nothing leaves the host.
set -euo pipefail
[[ $EUID == 0 ]] || exit 1
ops=$(cd -- "$(dirname -- "$0")/.." && pwd)
repo=$(cd -- "$ops/.." && pwd)
[[ ! -e /etc/psy-backup/passphrase ]] || { echo "A configured backup host; use a fresh CI host" >&2; exit 1; }
for unit in $(python3 -I "$ops/inventory.py" idle); do
    ! systemctl is-active --quiet "$unit" || { echo "$unit active; use a fresh CI host" >&2; exit 1; }
done
h=/home/hermes/.hermes
dest=/srv/ci-backup-dest
work=$(mktemp -d /var/tmp/psy-ci-restore.XXXXXX)
trap 'rm -rf -- "$work" "$dest" /etc/psy-backup/passphrase' EXIT
install -d -m 0700 "$dest"
(umask 077; head -c 48 /dev/urandom | base64 > /etc/psy-backup/passphrase)

# Synthetic profile, a WAL database and recordings, written by their owner.
runuser -u hermes -- python3 -I - "$h" <<'PY'
import os, sqlite3, sys
from pathlib import Path
h = Path(sys.argv[1])
os.umask(0o077)
for name, text in (('config.yaml', 'synthetic: true\n'), ('.env', 'SYNTHETIC=1\n'), ('auth.json', '{}\n')):
    if not (h / name).exists():
        (h / name).write_text(text)
db = sqlite3.connect(h / 'state.db')
db.execute('PRAGMA journal_mode=WAL')
db.execute('CREATE TABLE IF NOT EXISTS fixture(value TEXT)')
db.execute("INSERT INTO fixture VALUES ('synthetic')")
db.commit()
db.close()
media = h / 'media-vault/uploads'
(media / 'ci-recording.ogg').write_bytes(os.urandom(3 * 1024 * 1024))
(media / 'ci-job').mkdir(exist_ok=True)
(media / 'ci-job/ci-recording.txt').write_text('synthetic transcript\n')
PY

# The unit's own [Service] settings, as the timer would run it.
mapfile -t sandbox < <(sed -n '/^\[Service\]/,$p' /etc/systemd/system/psy-backup.service | grep -E '^[A-Za-z]+=' \
    | grep -v -E '^(Type|ExecStart|TimeoutStartSec)=')
backup() {
    systemd-run --quiet --wait --pipe --collect "${sandbox[@]/#/-p}" -p "Environment=PSY_BACKUP_TEST_DEST=$dest $*" \
        -- /usr/local/sbin/psy-backup
}
backup
[[ $(find "$dest/media" -name '*.gpg' | wc -l) == 2 && -f $dest/README.txt && -s /var/lib/psy-backup/last-success ]]
before=$(stat -c '%n %Y %s' "$dest"/media/*.gpg)
sleep 1
backup PSY_BACKUP_TEST_RESEND=1
# Sunday path: every recording offered again one at a time; existing copies are kept as they are.
[[ $(stat -c '%n %Y %s' "$dest"/media/*.gpg) == "$before" ]]
[[ $(find "$dest/daily" -name '*.tar.zst.gpg' | wc -l) == 2 ]]

archive=$(find "$dest/daily" -name '*.tar.zst.gpg' | sort | tail -n 1)
python3 "$repo/tools/verify_backup.py" "$archive" --passphrase-file /etc/psy-backup/passphrase --stage "$work/verified"
python3 -I - "$work/verified/db/state.db" <<'PY'
import sqlite3, sys
db = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)
assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete', 'snapshot must not need -wal/-shm'
assert db.execute('SELECT count(*) FROM fixture').fetchone()[0] >= 1
PY
install -d -m 0700 "$work/uploads"
python3 "$repo/tools/restore_media.py" --manifest "$work/verified/meta/media-manifest.tsv" --encrypted "$dest/media" \
    --passphrase-file /etc/psy-backup/passphrase --target "$work/uploads"
cmp "$h/media-vault/uploads/ci-recording.ogg" "$work/uploads/ci-recording.ogg"
cmp "$h/media-vault/uploads/ci-job/ci-recording.txt" "$work/uploads/ci-job/ci-recording.txt"
rm -rf -- "$h/media-vault/uploads/ci-recording.ogg" "$h/media-vault/uploads/ci-job"
echo "PASS: sandboxed backup to a local destination, Sunday resend kept existing copies, archive verified, recordings restored"
