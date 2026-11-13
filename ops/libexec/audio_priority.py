"""Give live recordings priority over the optional, resumable official Bot API build."""
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

HOME=Path('/home/hermes/.hermes')
STATE=Path('/var/lib/psy-audio-priority/state.json')
BUILD='psy-telegram-build.service'

def audio_busy():
    db=HOME/'vault/uploads/state.sqlite3'
    busy=False
    if db.exists():
        with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=3) as c:
            busy=bool(c.execute("SELECT EXISTS(SELECT 1 FROM jobs WHERE state IN ('queued','processing','sending') OR (state='uploading' AND updated>?))",(int(time.time())-120,)).fetchone()[0])
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
    state,action=decision(state,audio_busy(),props.get('ActiveState'),props.get('FreezerState'),int(time.time()))
    if action:
        subprocess.run(['systemctl',action,BUILD],check=True,timeout=10)
        print('Optional Telegram build paused for audio processing' if action=='freeze' else 'Optional Telegram build resumed after audio processing')
    tmp=STATE.with_suffix('.tmp');tmp.write_text(json.dumps(state));tmp.chmod(0o600);tmp.replace(STATE)

if __name__=='__main__':
    try:main()
    except Exception as e:
        # Keep the previous ownership marker after failures; never blindly thaw a manual pause.
        print('Audio priority check failed:',type(e).__name__)
        raise SystemExit(1)
