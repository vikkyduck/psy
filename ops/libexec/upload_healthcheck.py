"""External recovery for the upload services; does not restart the Hermes gateway.

Restarts are decided only from what this host can see: the uploader answering /healthz on its
own listener over loopback, and the worker and recognizer being active. The public HTTPS origin
is probed too, but a failure there (DNS, routing, firewall, certificate) cannot be fixed by a
restart, so it only marks the state unhealthy for the owner alerts. The state is saved before a
restart, and a failed restart backs off instead of being retried every minute.
"""
import argparse
import http.client
import importlib.util
import json
import os
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


def _common(default="/usr/local/lib/psy-common/psy_common.py"):
    """The shared PSY helpers, loaded by explicit path and never through sys.path: `default`, the
    root-owned installed copy unless a release tool passes the one shipped with it, or in tests
    the copy PSY_COMMON_PATH names."""
    module = sys.modules.get("psy_common")
    if module is None:
        path = str(os.environ.get("PSY_COMMON_PATH") or default)
        spec = importlib.util.spec_from_file_location("psy_common", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module = sys.modules.setdefault("psy_common", module)
    return module


common = _common()

ROOT = Path('/var/lib/psy-upload-health')
FAILS_BEFORE_RESTART = 3
PUBLIC_FAILS_BEFORE_ALERT = 3
COOLDOWN = 900            # after a restart; doubles after each failed restart, up to 8x


def active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit], timeout=10).returncode == 0


def healthz(host, port, context):
    connection = http.client.HTTPSConnection(host, port, timeout=8, context=context)
    try:
        connection.request('GET', '/healthz')
        response = connection.getresponse()
        return response.status == 200 and json.loads(response.read(4096)).get('ok') is True
    finally:
        connection.close()


def local_ok():
    """The uploader's own listener on this host. TLS without verification: only liveness matters."""
    host = os.environ.get('PSY_LISTEN_HOST', '0.0.0.0')
    port = int(os.environ.get('PSY_LISTEN_PORT', '9443'))
    host = {'0.0.0.0': '127.0.0.1', '': '127.0.0.1', '::': '::1', '[::]': '::1'}.get(host, host)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        return healthz(host, port, context)
    except Exception:
        return False


def public_ok():
    try:
        public = urlsplit(common.https_origin(os.environ.get('PSY_PUBLIC_URL', '')))
        return healthz(public.hostname, public.port or 443, ssl.create_default_context())
    except Exception:
        return False


def queue_report(state):
    try:
        pid = subprocess.check_output(['systemctl', 'show', 'psy-upload', '-p', 'MainPID', '--value'],
                                      text=True, timeout=5).strip()
        if not pid.isdecimal() or int(pid) < 2:
            raise ValueError('uploader unavailable')
        proc = subprocess.run(['nsenter', '-t', pid, '-m', '--', 'runuser', '-u', 'psy-upload', '--', '/usr/bin/python3', '-I',
                               '/opt/psy-upload/queue_health.py'], capture_output=True, check=True, timeout=8, cwd='/')
        queue = json.loads(proc.stdout)
        degraded = (queue['disk_low'] or queue['progress_warning'] or queue['oldest_delivery_seconds'] > 900
                    or queue['unconfirmed_deliveries'] > 0 or (queue['oldest_queue_seconds'] > 900 and not queue['processing']))
        state['queue'] = queue
        state['queue_status'] = 'attention' if degraded else 'ok'
    except Exception:
        state.pop('queue', None)
        state['queue_status'] = 'unavailable'
    if state['queue_status'] != 'ok':
        print('Queue or Telegram delivery needs attention:', state['queue_status'])
        if state.get('queue', {}).get('disk_low'):
            print('Low upload capacity after reservations')
        if state.get('queue', {}).get('progress_warning'):
            print('ASR has not advanced in 35 minutes')


def decide(state, now, api_ok, worker_ok, asr_ok, public):
    """Update counters; return the units to restart now (possibly none)."""
    state.update(api_ok=api_ok, worker_ok=worker_ok, asr_ok=asr_ok, public_ok=public)
    local = api_ok and worker_ok and asr_ok
    state['fails'] = 0 if local else state.get('fails', 0) + 1
    state['public_fails'] = 0 if public else state.get('public_fails', 0) + 1
    state['healthy'] = (state['fails'] < 2 and state['public_fails'] < PUBLIC_FAILS_BEFORE_ALERT
                        and state.get('queue_status') == 'ok')
    cooldown = COOLDOWN * 2 ** min(state.get('restart_failures', 0), 3)
    if state['fails'] < FAILS_BEFORE_RESTART or now - state.get('last_restart', 0) < cooldown:
        return []
    return [unit for unit, ok in (('psy-upload', api_ok), ('psy-upload-worker', worker_ok), ('psy-asr', asr_ok)) if not ok]


def save(path, state):
    common.atomic_write(path, json.dumps(state).encode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-only', action='store_true', help='probe and print; no restart, no state change')
    args = parser.parse_args()
    os.umask(0o077)
    ROOT.mkdir(exist_ok=True, mode=0o700)
    path = ROOT / 'state.json'
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    now = time.time()
    queue_report(state)
    units = decide(state, now, local_ok(), active('psy-upload-worker'), active('psy-asr'), public_ok())
    state['checked_at'] = int(now)
    print(f"upload health: local={state['api_ok']} worker={state['worker_ok']} asr={state['asr_ok']} "
          f"public={state['public_ok']} public_fails={state['public_fails']} fails={state['fails']} "
          f"queue={state['queue_status']} restart={','.join(units) or 'none'}")
    if args.check_only:
        return 0
    if not units:
        save(path, state)
        return 0
    # Record the attempt first: if the restart hangs or this process is killed, the next run
    # still sees the cooldown instead of restarting again a minute later.
    state.update(last_restart=now, restart_units=units, fails=0)
    save(path, state)
    try:
        result = subprocess.run(['systemctl', 'restart', *units], timeout=150).returncode
    except subprocess.TimeoutExpired:
        result = -1
    if result:
        state['restart_failures'] = state.get('restart_failures', 0) + 1
        save(path, state)
        print(f"Restart of {', '.join(units)} failed (attempt {state['restart_failures']}); next try after the backoff")
        return 1
    state['restart_failures'] = 0
    save(path, state)
    print(f"Recovered {', '.join(units)} after {FAILS_BEFORE_RESTART} failed local checks")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
