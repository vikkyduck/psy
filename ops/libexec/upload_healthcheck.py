"""External recovery for the upload services; does not restart the Hermes gateway."""
import http.client,json,os,ssl,subprocess,time
from pathlib import Path
from urllib.parse import urlsplit

def active(unit):
    return subprocess.run(['systemctl','is-active','--quiet',unit]).returncode==0


def main():
    os.umask(0o077)
    root=Path('/var/lib/psy-upload-health');root.mkdir(exist_ok=True,mode=0o700)
    path=root/'state.json'
    state=json.loads(path.read_text()) if path.exists() else {'fails':0,'last_restart':0}
    ok=False
    try:
        public = urlsplit(os.environ['PSY_PUBLIC_URL'])
        if public.scheme != 'https' or not public.hostname or public.username or public.password:
            raise ValueError('Invalid public HTTPS origin')
        connection=http.client.HTTPSConnection(public.hostname,public.port or 443,timeout=8,context=ssl.create_default_context())
        connection.request('GET','/healthz');response=connection.getresponse()
        ok=response.status==200 and json.loads(response.read(4096)).get('ok') is True
        connection.close()
    except Exception:pass
    api_ok=ok
    worker_ok=active('psy-upload-worker')
    asr_ok=active('psy-asr')
    ok=api_ok and worker_ok and asr_ok
    try:
        pid = subprocess.check_output(['systemctl','show','psy-upload','-p','MainPID','--value'],text=True,timeout=3).strip()
        if not pid.isdecimal() or int(pid)<2:
            raise ValueError('uploader unavailable')
        proc = subprocess.run(['nsenter','-t',pid,'-m','--','runuser','-u','psy-upload' ,'--','/usr/bin/python3','-I',
                               '/opt/psy-upload/queue_health.py'],capture_output=True,check=True,timeout=8,cwd='/')
        queue = json.loads(proc.stdout)
        degraded = (queue['disk_low'] or queue['progress_warning'] or queue['oldest_delivery_seconds'] > 900 or queue['unconfirmed_deliveries'] > 0
                    or (queue['oldest_queue_seconds'] > 900 and not queue['processing']))
        state['queue'] = queue
        state['queue_status'] = 'attention' if degraded else 'ok'
    except Exception:
        state.pop('queue',None)
        state['queue_status']='unavailable'
    if state['queue_status']!='ok':
        print('Queue or Telegram delivery needs attention:',state['queue_status'])
        if state.get('queue',{}).get('disk_low'):
            print('Low upload capacity after reservations')
        if state.get('queue',{}).get('progress_warning'):
            print('ASR has not advanced in 35 minutes')
    state.update(api_ok=api_ok,worker_ok=worker_ok,asr_ok=asr_ok)
    state['fails']=0 if ok else state['fails']+1
    if state['fails']>=3 and time.time()-state['last_restart']>900:
        units=['psy-upload'] if not api_ok else []
        if not worker_ok:units.append('psy-upload-worker')
        if not asr_ok:units.append('psy-asr')
        subprocess.run(['systemctl','restart',*units],check=True)
        state.update(last_restart=time.time(),fails=0)
        print('Recovered upload service after 3 failed checks')
    state['checked_at']=int(time.time());state['healthy']=ok and state['queue_status']=='ok'
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(state));tmp.chmod(0o600);tmp.replace(path)

if __name__=='__main__':main()
