'use strict';

// This transfer function is also exercised by the interrupted-network regression tests.
function uploadPaused(){const e=new Error('Загрузка приостановлена.');e.name='AbortError';return e;}
function uploadWait(ms,signal){
  return new Promise((resolve,reject)=>{
    if(signal?.aborted){reject(uploadPaused());return;}
    let timer;
    const done=()=>{clearTimeout(timer);signal?.removeEventListener('abort',stop);resolve();};
    const stop=()=>{clearTimeout(timer);signal?.removeEventListener('abort',stop);reject(uploadPaused());};
    timer=setTimeout(done,ms);signal?.addEventListener('abort',stop,{once:true});
  });
}
async function recordingIdentity(file,signal){
  const block=1024*1024;
  const hash=data=>crypto.subtle.digest('SHA-256',data);
  const hex=bytes=>Array.from(new Uint8Array(bytes),x=>x.toString(16).padStart(2,'0')).join('');
  const parts=new Uint8Array(32*Math.ceil(file.size/block));
  let fingerprint;
  for(let pos=0;pos<file.size;pos+=block){
    if(signal?.aborted)throw uploadPaused();
    const bytes=await file.slice(pos,pos+block).arrayBuffer();
    if(pos===0)fingerprint=hex(await hash(bytes.slice(0,65536)));
    parts.set(new Uint8Array(await hash(bytes)),32*(pos/block));
  }
  return {fingerprint,content_id:hex(await hash(parts))};
}
async function transferRecording({file,language,request,signal,chunkSize=1024*1024,onState=()=>{},wait=uploadWait,requestTimeout=90000}){
  let offset=0,job=null,attempt=0,conflict=null;
  const stopped=()=>{if(signal?.aborted)throw uploadPaused();};
  const emit=(phase,extra={})=>onState({phase,offset,size:file.size,jobId:job?.id,...extra});
  const temporary=e=>!e.status||[408,425,429,500,502,503,504].includes(e.status);
  async function call(path,options={}){
    stopped();const ctrl=new AbortController();
    const abort=()=>ctrl.abort();signal?.addEventListener('abort',abort,{once:true});
    const timer=setTimeout(()=>ctrl.abort(),requestTimeout);
    try{return await request(path,{...options,signal:ctrl.signal});}
    finally{clearTimeout(timer);signal?.removeEventListener('abort',abort);stopped();}
  }
  async function reconnect(e){
    if(!temporary(e))throw e;
    stopped();attempt++;
    emit('retrying',{attempt,reason:e.status===429?'queue':'network'});
    await wait(Math.min(15000,1000*2**Math.min(attempt-1,4)),signal);stopped();
  }
  const accepted=j=>['queued','processing','delivery_pending','sending','done'].includes(j.state);
  const validOffset=value=>Number.isInteger(value)&&value>=0&&value<=file.size;
  function readJob(j){
    if(!j||!validOffset(j.offset)){const e=new Error('Не удалось сверить сохраненную часть файла.');e.status=400;throw e;}
    offset=j.offset;job=j;
    if(accepted(j)){offset=file.size;return true;}
    if(j.state!=='uploading'){const e=new Error('Эта загрузка уже остановлена. Выберите файл заново.');e.status=400;throw e;}
    return false;
  }
  emit('preparing');
  const identity=await recordingIdentity(file,signal);
  // The server serializes creation by verified content, user and destination.
  while(!job){
    try{
      const created=await call('/api/jobs',{method:'POST',body:JSON.stringify({filename:file.name,size:file.size,...identity,language})});
      if(readJob({state:'uploading',...created})){emit('complete');return job;}
    }catch(e){await reconnect(e);}
  }
  let synchronize=false;
  while(offset<file.size||synchronize){
    stopped();
    try{
      if(synchronize){
        const list=await call('/api/jobs');
        if(readJob(list.jobs.find(j=>j.id===job.id))){emit('complete');return job;}
        if(conflict)throw Object.assign(conflict,{status:400});
        synchronize=false;
        if(offset===file.size)break;
      }
      emit('uploading');
      const end=Math.min(offset+Math.min(chunkSize,1024*1024),file.size);
      const result=await call('/api/jobs/'+job.id,{method:'PATCH',headers:{'Content-Type':'application/octet-stream','Upload-Offset':String(offset)},body:file.slice(offset,end)});
      if(!validOffset(result.offset)||result.offset<=offset)throw Object.assign(new Error('Не удалось подтвердить передачу части файла.'),{status:400});
      offset=result.offset;attempt=0;emit('uploading');
    }catch(e){
      if(signal?.aborted)throw uploadPaused();
      if(e.status===409){
        // A saved chunk can lose its reply. Use the server's offset, never append blindly.
        if(Number.isInteger(e.offset)&&validOffset(e.offset))offset=e.offset;
        else conflict=e;
        synchronize=true;continue;
      }
      await reconnect(e);synchronize=true;
    }
  }
  // Completion is idempotent, including a reply lost after the server queued the recording.
  for(;;){
    stopped();emit('finalizing');
    try{
      const result=await call('/api/jobs/'+job.id+'/complete',{method:'POST'});
      if(!accepted(result))throw Object.assign(new Error('Не удалось передать запись на обработку.'),{status:400});
      emit('complete');return {...job,...result,offset:file.size};
    }catch(e){await reconnect(e);}
  }
}
if(typeof module!=='undefined'&&module.exports)module.exports={transferRecording,uploadWait,recordingIdentity};

if(typeof document!=='undefined'){
const $=id=>document.getElementById(id),tg=window.Telegram?.WebApp;
let session='',jobs=[],busy=false,paused=false,controller=null,chunk=1024*1024,activeJob=null,completedFile=null,wakeLock=null,refreshing=false;
const labels={uploading:'Загружается',queued:'В очереди на расшифровку',processing:'Расшифровываем запись',delivery_pending:'Текст сохранен, ожидает отправки',sending:'Отправляем результат',done:'Обработка завершена',failed:'Обработка не завершена',cancelled:'Загрузка отменена'};
function note(text){$('notice').textContent=text;}
async function api(path,options={}){
  const headers={'X-Upload-Session':session,...options.headers};
  if(options.body&&typeof options.body==='string')headers['Content-Type']='application/json';
  const r=await fetch(path,{...options,headers});
  if(!r.ok){let b;try{b=await r.json();}catch{b={};}const e=new Error(b.error||'Не удалось связаться с сервером.');e.status=r.status;e.offset=b.offset;throw e;}
  return r;
}
async function refresh(){
  if(!session||refreshing)return;
  refreshing=true;const ctrl=new AbortController(),timer=setTimeout(()=>ctrl.abort(),15000);
  try{jobs=(await(await api('/api/jobs',{signal:ctrl.signal})).json()).jobs;render();}catch(e){if(e.status===401&&!busy)note('Срок входа истек. Закройте окно и откройте его кнопкой в Telegram.');}
  finally{clearTimeout(timer);refreshing=false;}
}
async function refreshCapacity(){
  const ctrl=new AbortController(),timer=setTimeout(()=>ctrl.abort(),15000);
  try{
    const storage=await(await api('/api/capacity',{signal:ctrl.signal})).json();
    const mib=bytes=>Math.floor(bytes/(1024**2)).toLocaleString('ru-RU');
    const lines=['Личный лимит хранения: '+mib(storage.member_limit_bytes)+' МиБ.'];
    if(storage.member_low){
      lines.push(storage.member_upload_bytes
        ? 'С учетом резерва на текст для новой записи доступно до '+mib(storage.member_upload_bytes)+' МиБ.'
        : 'Личный лимит исчерпан для новых загрузок.');
    }
    if(storage.disk_low)lines.push('Свободного места на сервере может не хватить для записи размером 1 ГиБ.');
    $('capacity').textContent=lines.join(' ');
  }catch{$('capacity').textContent='Не удалось проверить свободное место.';}
  finally{clearTimeout(timer);}
}
function render(){
  $('history').hidden=jobs.length===0;$('jobs').replaceChildren();
  for(const j of jobs){
    const box=document.createElement('div');box.className='job';
    const title=document.createElement('strong');title.textContent=j.filename;box.append(title);
    const status=document.createElement('p');status.textContent=(labels[j.state]||j.state)+' · '+(j.size/1024/1024).toFixed(1)+' МБ';box.append(status);
    if(j.is_repeat){const p=document.createElement('p');p.textContent='Повторная расшифровка';box.append(p);}
    if(j.state==='processing'&&j.progress){
      const clock=seconds=>Math.floor(seconds/60)+':'+String(Math.floor(seconds%60)).padStart(2,'0');
      const p=document.createElement('p');p.textContent='Обработано '+clock(j.progress.processed_seconds)+' из '+clock(j.progress.total_seconds)+' · '+j.progress.percent+'%';box.append(p);
      const progress=document.createElement('progress');progress.max=100;progress.value=j.progress.percent;box.append(progress);
    }
    if(j.progress_warning){
      const p=document.createElement('p');
      p.textContent='Более 35 минут нет новых обработанных фрагментов. После 45 минут ожидания распознавание остановится.';
      box.append(p);
    }
    if(j.state==='uploading'){
      const info=document.createElement('p');
      info.textContent=Math.round(j.offset/j.size*100)+'% сохранено. '+(busy&&j.id===activeJob?'Загрузка продолжается автоматически.':paused&&j.id===activeJob?'Вы приостановили загрузку. Нажмите "Продолжить загрузку".':'Для продолжения выберите этот же файл выше и нажмите "Загрузить и расшифровать".');box.append(info);
      if(!busy){const b=document.createElement('button');b.className='secondary';b.textContent='Отменить незавершенную загрузку';b.onclick=async()=>{try{await api('/api/jobs/'+j.id+'/cancel',{method:'POST'});await refresh();}catch(e){note(e.message);}};box.append(b);}
    }
    if(j.error){const p=document.createElement('p');p.textContent=j.error;box.append(p);}
    if(['delivery_pending','sending','done'].includes(j.state)){
      if(j.state==='done'){
        const p=document.createElement('p');p.textContent=j.delivery==='sent'?'Файл отправлен в Telegram.':'Текст сохранен. Доставка в Telegram не подтверждена.';box.append(p);
        if(j.delivery!=='sent'){
          const resend=document.createElement('button');resend.className='secondary';resend.textContent='Повторить отправку в чат';
          resend.onclick=async()=>{
            if(j.delivery==='unknown'&&!window.confirm('Telegram мог уже получить этот файл. Отправить еще раз?'))return;
            resend.disabled=true;
            try{await api('/api/jobs/'+j.id+'/redeliver',{method:'POST',body:JSON.stringify({confirm_possible_duplicate:j.delivery==='unknown'})});await refresh();}
            catch(e){note(e.message);resend.disabled=false;}
          };
          box.append(resend);
        }
      }
      const b=document.createElement('button');b.className='secondary';b.textContent='Скачать расшифровку';
      b.onclick=async()=>{try{const r=await api('/api/jobs/'+j.id+'/transcript');const u=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=u;a.download='Расшифровка.txt';a.click();setTimeout(()=>URL.revokeObjectURL(u),60000);}catch(e){note(e.message);}};box.append(b);
      if(j.state==='done'&&!j.has_next){
        const repeat=document.createElement('button');
        repeat.className='secondary';repeat.textContent='Расшифровать заново';
        repeat.onclick=async()=>{
          repeat.disabled=true;
          try{
            await api('/api/jobs/'+j.id+'/reprocess',{method:'POST'});
            note('Повторная расшифровка есть в списке записей. Предыдущий текст можно скачать.');
            await Promise.all([refresh(),refreshCapacity()]);
          }catch(e){note(e.message);repeat.disabled=false;}
        };
        box.append(repeat);
      }
    }
    if(j.state==='failed'){
      const b=document.createElement('button');b.className='secondary';b.textContent='Повторить обработку';b.onclick=async()=>{b.disabled=true;try{await api('/api/jobs/'+j.id+'/retry',{method:'POST',body:JSON.stringify({retry_generation:j.retry_generation})});await refresh();}catch(e){note(e.message);b.disabled=false;}};box.append(b);
    }
    $('jobs').append(box);
  }
}
async function keepAwake(){
  if(busy&&!wakeLock&&document.visibilityState==='visible'){
    try{wakeLock=await navigator.wakeLock?.request('screen');wakeLock?.addEventListener('release',()=>{wakeLock=null;});}catch{}
  }
}
document.addEventListener('visibilitychange',keepAwake);
$('file').onchange=()=>{
  completedFile=null;paused=false;activeJob=null;$('progressbox').hidden=true;
  $('filename').textContent=$('file').files[0]?.name||'Выберите запись';
  $('send').textContent='Загрузить и расшифровать';$('send').disabled=busy||!$('file').files.length;
};
$('pause').onclick=()=>{paused=true;controller?.abort();};
$('send').onclick=async()=>{
  const file=$('file').files[0];if(!file||busy||file===completedFile)return;
  if(file.size>1024**3){note('Выберите запись размером до 1 ГБ.');return;}
  busy=true;paused=false;controller=new AbortController();
  $('send').disabled=true;$('send').textContent='Загрузка идет…';$('file').disabled=true;$('language').disabled=true;
  $('progressbox').hidden=false;$('pause').hidden=false;
  note('Подготавливаем запись. Повторно нажимать кнопку не нужно.');
  tg?.enableClosingConfirmation();await keepAwake();
  try{
    await transferRecording({file,language:$('language').value,chunkSize:chunk,signal:controller.signal,
      request:async(path,options)=>await(await api(path,options)).json(),
      onState:s=>{
        activeJob=s.jobId||activeJob;
        $('progress').value=s.offset/file.size*100;
        $('progresslabel').textContent=(s.offset/1024/1024).toFixed(1)+' из '+(file.size/1024/1024).toFixed(1)+' МБ';
        if(s.phase==='retrying'){
          note(s.reason==='queue'?'Ждем свободное место в очереди. Загрузка продолжится автоматически.':'Связь прервалась. Восстанавливаю загрузку автоматически - повторно нажимать кнопку не нужно.');
          $('send').textContent='Восстанавливаем связь…';
        }else if(s.phase==='uploading'){
          note('Загрузка продолжается. Дождитесь ее завершения - все части передаются автоматически.');$('send').textContent='Загрузка идет…';
        }else if(s.phase==='finalizing'){note('Файл передан. Подтверждаем начало расшифровки…');}
        render();
      }});
    completedFile=file;$('progress').value=100;$('progresslabel').textContent='Загрузка завершена';
    note('Запись загружена. Окно можно закрыть - PSY обработает файл и пришлет расшифровку.');
    tg?.disableClosingConfirmation();
  }catch(e){
    note(paused?'Вы приостановили загрузку. Нажмите "Продолжить загрузку" - выбирать файл заново не нужно.':e.status===401?'Срок входа истек. Откройте загрузку заново через Telegram. Уже переданные части сохранены.':e.message+' Уже переданные части сохранены.');
  }finally{
    busy=false;controller=null;try{await wakeLock?.release();}catch{}wakeLock=null;
    $('send').disabled=completedFile===file;$('send').textContent=completedFile===file?'Запись загружена':paused?'Продолжить загрузку':'Загрузить и расшифровать';
    $('file').disabled=false;$('language').disabled=false;$('pause').hidden=true;
    await Promise.all([refresh(),refreshCapacity()]);
  }
};
(async()=>{
  tg?.ready();tg?.expand();
  if(!tg?.initData){note('Для загрузки откройте кнопку "Записи" в закрепленном сообщении группы или в личной переписке с ботом.');return;}
  try{
    const r=await api('/api/auth',{method:'POST',body:JSON.stringify({initData:tg.initData,ticket:new URLSearchParams(location.search).get('ticket')})});
    const b=await r.json();session=b.session;chunk=Math.min(b.chunk,1024*1024);$('destination').textContent=b.destination;$('upload').hidden=false;
    note('Доступ подтвержден. Выберите запись и нажмите "Загрузить и расшифровать" один раз.');
    await Promise.all([refresh(),refreshCapacity()]);setInterval(refresh,8000);setInterval(refreshCapacity,60000);
  }catch(e){note(e.message);}
})();
}
