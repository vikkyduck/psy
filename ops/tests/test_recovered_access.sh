#!/bin/bash
# Fresh CI/staging host only. Read checks use real system users and isolated mounts.
set -euo pipefail
[[ $EUID == 0 ]] || exit 1
ops=$(cd -- "$(dirname -- "$0")/.." && pwd)
bash "$ops/restore-permissions"
h=/home/hermes/.hermes
tag=.recovered-fixture-$(cat /proc/sys/kernel/random/uuid)
media=$h/media-vault/uploads/$tag
queue=$h/vault/uploads/$tag
journal=$h/psy-codex-renewals/$tag.json
trap 'rm -f -- "$media" "$queue" "$journal"' EXIT
runuser -u hermes -- sh -ec 'umask 077; echo synthetic-recovered > "$1"; echo fixture > "$2"; chmod 0600 "$1" "$2"' sh "$media" "$queue"
install -d -m 0700 "$h/psy-codex-renewals"
printf '{}\n' > "$journal"
chmod 0600 "$journal"
bash "$ops/restore-permissions"
runuser -u hermes -- test -r "$journal"
systemd-run --quiet --wait --pipe --collect -p User=psy-asr -p Group=psy-deliver \
    -p ProtectHome=yes -p ProtectSystem=strict -p NoNewPrivileges=yes -p PrivateNetwork=yes \
    -p "BindReadOnlyPaths=$h/media-vault/uploads:/srv/psy-media" \
    /bin/sh -ec 'test -r "$1"; test ! -w "$1"; test ! -r /home/hermes/.hermes/auth.json' sh "/srv/psy-media/$tag"
systemd-run --quiet --wait --pipe --collect -p User=psy-upload -p Group=psy-upload \
    -p ProtectHome=yes -p ProtectSystem=strict -p NoNewPrivileges=yes -p PrivateNetwork=yes \
    -p "BindPaths=$h/vault/uploads:/srv/psy-queue" \
    /bin/sh -ec 'test -r "$1"; test -w "$1"; test ! -r /etc/psy-upload/recording-signing' sh "/srv/psy-queue/$tag"
echo "PASS: recovered pre-existing files have named ACLs without opening OAuth or signing credentials"
