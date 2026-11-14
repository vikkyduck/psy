PSY private backup layout

daily/*.tar.zst.gpg contains private settings, SQLite snapshots and recovery state.
media/<sha256>.gpg contains each distinct recording or result file once.
meta/media-manifest.tsv inside the daily archive binds private destination paths to hashes.
This README contains no credentials. Keep the encryption passphrase separately from the server and backup host.

Start with docs/RECOVERY.md in the public PSY source repository.
Verify a downloaded archive using tools/verify_backup.py in a private directory.
Restore onto an idle host. Never start a second Telegram poller or reuse stale rotated OAuth state.
Do not extract a full archive over a running server. Inspect and restore only the listed PSY state.
