"""Give live recordings priority over the optional, resumable official Bot API build."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


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

HOME=Path('/home/hermes/.hermes')
STATE=Path('/var/lib/psy-audio-priority/state.json')
BUILD='psy-telegram-build.service'

def audio_busy():
    """True or False; None when the queue cannot be read (the caller then changes nothing)."""
    db=HOME/'vault/uploads/state.sqlite3'
    busy=False
    if db.exists():
        try:
            # Root never creates files next to the queue: -wal/-shm it made would lock out the services.
            c=common.open_readonly(db)
            try:busy=bool(c.execute("SELECT EXISTS(SELECT 1 FROM jobs WHERE state IN ('queued','processing','sending') OR (state='uploading' AND updated>?))",(int(time.time())-120,)).fetchone()[0])
            finally:c.close()
        except (OSError,sqlite3.Error):
            return None
    lock=HOME/'cache/scratch/audio-transcription.lock'
    if lock.exists():
        with lock.open('rb') as f:
            try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:busy=True
            else:fcntl.flock(f,fcntl.LOCK_UN)
    return busy

def decision(state,busy,active,freezer,now):
    state={**state,'checked_at':now,'audio_busy':busy}
    if active!='active':
        state.update(managed_freeze=False,idle_since=None)
        return state,None
    if busy:
        state['idle_since']=None
        if freezer=='running':
            state['managed_freeze']=True
            return state,'freeze'
    elif state.get('managed_freeze'):
        if freezer=='running':state.update(managed_freeze=False,idle_since=None)
        else:
            if state.get('idle_since') is None:state['idle_since']=now
            if now-state['idle_since']>=60:
                state.update(managed_freeze=False,idle_since=None)
                return state,'thaw'
    return state,None

def main():
    os.umask(0o077);STATE.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    state=json.loads(STATE.read_text()) if STATE.exists() else {'managed_freeze':False,'idle_since':None}
    raw=subprocess.check_output(['systemctl','show',BUILD,'-p','ActiveState','-p','FreezerState'],text=True,timeout=5)
    props=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
    busy=audio_busy()
    if busy is None:
        # Neither freeze nor thaw on a guess; the previous state and any pause stay as they are.
        print('Audio priority check skipped: recording queue unreadable')
        return
    state,action=decision(state,busy,props.get('ActiveState'),props.get('FreezerState'),int(time.time()))
    if action:
        subprocess.run(['systemctl',action,BUILD],check=True,timeout=10)
        print('Optional Telegram build paused for audio processing' if action=='freeze' else 'Optional Telegram build resumed after audio processing')
    common.atomic_write(STATE,json.dumps(state).encode())

if __name__=='__main__':
    try:main()
    except Exception as e:
        # Keep the previous ownership marker after failures; never blindly thaw a manual pause.
        print('Audio priority check failed:',type(e).__name__)
        raise SystemExit(1)
