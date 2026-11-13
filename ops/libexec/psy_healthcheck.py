#!/usr/bin/python3
"""External gateway watchdog. Never fetches/acknowledges Telegram updates."""
import argparse
import json
import pathlib
import os
import socket
import subprocess
import time
import urllib.request

HOME = pathlib.Path('/home/hermes/.hermes')
STATE = pathlib.Path('/var/lib/psy-healthcheck/state.json')
STALL = 300
COOLDOWN = 900


def control_status():
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(12)
        client.connect(str(HOME / 'gateway.sock'))
        client.sendall(b'{"verb":"status"}\n')
        data = b''
        while b'\n' not in data and len(data) < 524288:
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
    answer = json.loads(data)
    if not answer.get('ok'):
        raise ValueError('control status rejected')
    return answer['result']


def telegram_pending():
    token = None
    for line in (HOME / '.env').read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep and key.strip() == 'TELEGRAM_BOT_TOKEN':
            token = value.strip().strip('\"\'')
            break
    if not token:
        raise ValueError('missing bot credential')
    base = 'https://api.telegram.org/bot'
    request = urllib.request.Request(base + token + '/getWebhookInfo', data=b'')
    with urllib.request.urlopen(request, timeout=18) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise ValueError('Bot API rejected request')
    return int(result['result']['pending_update_count'])


def last_ingress():
    with (HOME / 'logs/gateway.log').open('rb') as log:
        log.seek(0, 2)
        log.seek(max(0, log.tell() - 262144))
        lines = log.read().decode('utf-8', errors='replace').splitlines()
    for line in reversed(lines):
        if 'inbound message: platform=telegram' in line:
            return line[:23]  # Timestamp only, never message content.
    return None


def assess(previous, now, pid, control_ok, connected, pending, ingress):
    state = dict(previous)
    if state.get('pid') != pid:
        state = {'pid': pid, 'last_restart': state.get('last_restart', 0)}
    state['control_failures'] = 0 if control_ok else state.get('control_failures', 0) + 1
    if connected:
        state.pop('disconnected_since', None)
    else:
        state.setdefault('disconnected_since', now)
    if ingress and ingress != state.get('ingress'):
        state['ingress'] = ingress
        state['progress_at'] = now
    if pending is not None and pending > 0:
        state.setdefault('backlog_since', now)
        state.setdefault('progress_at', now)
    elif pending == 0:
        state.pop('backlog_since', None)
    reason = None
    if state['control_failures'] >= 3:
        reason = 'gateway_event_loop_unresponsive'
    elif not connected and now - state.get('disconnected_since', now) >= STALL:
        reason = 'telegram_disconnected'
    elif pending and now - max(state.get('backlog_since', now), state.get('progress_at', now)) >= STALL:
        reason = 'telegram_backlog_without_ingress_progress'
    if reason and now - state.get('last_restart', 0) < COOLDOWN:
        reason = None
    return state, reason


def self_test():
    state = {}
    for tick in (1000, 1060, 1120):
        state, reason = assess(state, tick, 1, False, False, None, None)
    assert reason == 'gateway_event_loop_unresponsive'
    state, reason = assess({}, 1000, 1, True, True, 2, 'one')
    state, reason = assess(state, 1301, 1, True, True, 2, 'one')
    assert reason == 'telegram_backlog_without_ingress_progress'
    state, reason = assess(state, 1302, 1, True, True, 2, 'two')
    assert reason is None  # A busy group with real progress is healthy.
    state, reason = assess(state, 1700, 1, True, True, 0, 'two')
    assert reason is None
    state, reason = assess({}, 1000, 1, True, False, None, None)
    state, reason = assess(state, 1301, 1, True, False, None, None)
    assert reason == 'telegram_disconnected'
    state['last_restart'] = 1300
    assert assess(state, 1302, 1, False, False, 2, None)[1] is None
    assert assess(state, 1400, 2, True, True, 0, None)[1] is None
    print('PASS: stalled loop, disconnected adapter, stalled backlog, healthy traffic, empty queue, cooldown, PID change')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    props = subprocess.check_output(['systemctl', 'show', 'hermes-gateway',
        '-p', 'MainPID', '-p', 'ActiveState', '-p', 'ActiveEnterTimestampMonotonic'], text=True)
    props = dict(line.split('=', 1) for line in props.splitlines() if '=' in line)
    if props.get('ActiveState') != 'active':
        print('SKIP: gateway not active; systemd owns process recovery')
        return
    age = time.monotonic() - int(props.get('ActiveEnterTimestampMonotonic') or 0) / 1000000
    if age < 180:
        print('SKIP: startup grace period')
        return
    pid = int(props['MainPID'])
    control_ok, connected = False, False
    try:
        status = control_status()
        control_ok = status.get('answering_pid') == pid
        connected = control_ok and status.get('platforms', {}).get('telegram', {}).get('state') == 'connected'
    except Exception:
        pass
    pending = None
    try:
        pending = telegram_pending()
    except Exception:
        pass  # Network failure alone must not cause a restart loop.
    try:
        ingress = last_ingress()
    except OSError:
        ingress = None
    try:
        previous = json.loads(STATE.read_text())
    except (OSError, ValueError):
        previous = {}
    now = time.time()
    state, reason = assess(previous, now, pid, control_ok, connected, pending, ingress)
    print(f'health: pid={pid} control={control_ok} telegram_connected={connected} pending={pending} recovery={reason or "none"}')
    if args.check_only:
        return
    if reason:
        state['last_restart'] = now
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(state))
    temporary.chmod(0o600)
    temporary.replace(STATE)
    if reason:
        print('RECOVERY: restarting hermes-gateway; reason=' + reason, flush=True)
        subprocess.run(['systemctl', 'restart', 'hermes-gateway'], timeout=100, check=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Exception text may contain Bot API URLs: report type only.
        print('healthcheck failed: ' + type(error).__name__)
        raise SystemExit(1)
