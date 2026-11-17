'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {File}=require('node:buffer');
const {createHash,webcrypto}=require('node:crypto');
const fs=require('node:fs');
const vm=require('node:vm');
const {transferRecording}=require('../static/app.js');
const MB=1024*1024;
const fault=status=>Object.assign(new Error('Injected failure'),status?{status}:{});
function fixture(size=26*MB+51){
  const bytes=Buffer.alloc(size);for(let n=0;n<size;n++)bytes[n]=(n*31+(n>>>16))%251;
  const file=new File([bytes],'network-test.wav');
  const fingerprint=createHash('sha256').update(bytes.subarray(0,65536)).digest('hex');
  return {file,bytes,fingerprint};
}
function server(){
  let job=null,parts=[],creates=0,queued=0;
  async function request(path,options={}){
    const method=options.method||'GET';
    if(path==='/api/jobs'&&method==='GET')return {jobs:job?[{...job}]:[]};
    if(path==='/api/jobs'&&method==='POST'){
      if(job&&JSON.parse(options.body).content_id===job.content_id)return {...job};
      creates++;job={...JSON.parse(options.body),id:'test-job',offset:0,state:'uploading'};return {id:job.id,offset:0};
    }
    if(method==='PATCH'){
      const offset=Number(options.headers['Upload-Offset']);
      if(offset!==job.offset)throw Object.assign(fault(409),{offset:job.offset});
      const data=Buffer.from(await options.body.arrayBuffer());parts.push(data);job.offset+=data.length;return {offset:job.offset};
    }
    if(path.endsWith('/complete')){if(job.state==='uploading'){queued++;job.state='queued';}return {...job};}
    throw fault(404);
  }
  return {request,get job(){return job;},get bytes(){return Buffer.concat(parts);},get creates(){return creates;},get queued(){return queued;}};
}

test('one invocation transfers >20 MiB despite lost creation, mid-file replies, offline reconciliation and lost completion',async()=>{
  const f=fixture(),s=server(),seen=new Set(),phases=[],delays=[];
  async function once(key,action){if(!seen.has(key)){seen.add(key);return action();}}
  const request=async(path,o={})=>{
    if(o.method==='POST'&&path==='/api/jobs'&&!seen.has('create'))return once('create',async()=>{await s.request(path,o);throw fault();});
    if(o.method==='PATCH'){
      const off=+o.headers['Upload-Offset'];
      if(off===4*MB&&!seen.has('before'))return once('before',()=>{throw fault(503);});
      if(off===13*MB&&!seen.has('after'))return once('after',async()=>{await s.request(path,o);throw fault();});
    }
    if(path==='/api/jobs'&&!o.method&&seen.has('after')&&!seen.has('sync'))return once('sync',()=>{throw fault(502);});
    if(path.endsWith('/complete')&&!seen.has('complete'))return once('complete',async()=>{await s.request(path,o);throw fault();});
    return s.request(path,o);
  };
  await transferRecording({...f,language:'ru',request,wait:async ms=>delays.push(ms),onState:s=>phases.push(s.phase)});
  assert.deepEqual(s.bytes,f.bytes);assert.equal(s.creates,1);assert.equal(s.queued,1);assert.equal(s.job.offset,f.file.size);
  assert.equal(phases.at(-1),'complete');assert.equal(delays.length,5);assert.ok(phases.includes('retrying'));
});

test('a stale offset is reconciled without duplicate or missing bytes',async()=>{
  const f=fixture(7*MB+31),s=server();let lost=false;
  const request=async(path,o={})=>{
    if(o.method==='PATCH'&&+o.headers['Upload-Offset']===3*MB&&!lost){lost=true;await s.request(path,o);throw Object.assign(fault(409),{offset:s.job.offset});}
    return s.request(path,o);
  };
  await transferRecording({...f,language:'ru',request,wait:async()=>{}});
  assert.deepEqual(s.bytes,f.bytes);assert.equal(s.creates,1);assert.equal(s.queued,1);
});

test('explicit pause stops automatic retry; resuming uses the same file and existing upload',async()=>{
  const f=fixture(6*MB+19),s=server(),ctrl=new AbortController();let failed=false;
  const request=async(path,o={})=>{
    if(o.method==='PATCH'&&+o.headers['Upload-Offset']===2*MB&&!failed){failed=true;throw fault();}
    return s.request(path,o);
  };
  await assert.rejects(transferRecording({...f,language:'ru',request,signal:ctrl.signal,wait:async()=>{ctrl.abort();}}),{name:'AbortError'});
  assert.equal(s.job.offset,2*MB);assert.equal(s.queued,0);
  await transferRecording({...f,language:'ru',request,wait:async()=>{}});
  assert.deepEqual(s.bytes,f.bytes);assert.equal(s.creates,1);assert.equal(s.queued,1);
});

test('permission errors stop immediately instead of retrying or weakening authorization',async()=>{
  let calls=0,waits=0;
  await assert.rejects(transferRecording({...fixture(MB),language:'ru',request:async()=>{calls++;throw fault(403);},wait:async()=>waits++}),{status:403});
  assert.equal(calls,1);assert.equal(waits,0);
});

test('a hanging request times out and recovers automatically',async()=>{
  const f=fixture(2*MB+7),s=server();let hung=false,retries=0;
  const request=async(path,o={})=>{
    if(o.method==='PATCH'&&!hung){hung=true;return new Promise((_,reject)=>o.signal.addEventListener('abort',()=>reject(Object.assign(new Error('timeout'),{name:'AbortError'})),{once:true}));}
    return s.request(path,o);
  };
  await transferRecording({...f,language:'ru',request,requestTimeout:20,wait:async()=>retries++});
  assert.deepEqual(s.bytes,f.bytes);assert.equal(retries,1);assert.equal(s.queued,1);
});

test('the actual UI needs one click, keeps the button locked during retry, and prevents duplicate resubmission',async()=>{
  class Element{constructor(){this.children=[];this.textContent='';this.files=[];this.value='';this.hidden=false;this.disabled=false;}append(x){this.children.push(x);}replaceChildren(){this.children=[];}}
  const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const f=fixture(5*MB+13),s=server();let failed=false;
  let prior={id:'finished',filename:'finished.wav',state:'done',delivery:'sent',size:123};
  let repeat=null;
  const context={console,AbortController,Blob,URL,crypto:webcrypto,setTimeout,clearTimeout,setInterval:()=>0,navigator:{},
    location:{search:''},URLSearchParams,
    document:{getElementById:get,createElement:()=>new Element(),addEventListener:()=>{},visibilityState:'hidden'},
    window:{Telegram:{WebApp:{initData:'isolated-test-data',ready(){},expand(){},enableClosingConfirmation(){},disableClosingConfirmation(){}}}},
    fetch:async(path,o={})=>{
      if(path==='/api/auth')return Response.json({session:'isolated-test-session',chunk:4*MB,destination:'Test'});
      if(path==='/api/capacity')return Response.json({disk_low:true});
      if((path==='/api/jobs'||path==='/api/jobs?count=20')&&!o.method)return Response.json({jobs:[prior,...[repeat,s.job].filter(Boolean)],more:false});
      if(path==='/api/jobs/finished/reprocess'&&o.method==='POST'){
        prior={...prior,has_next:true};
        repeat={id:'revision',filename:prior.filename,size:prior.size,state:'queued',is_repeat:true};
        return Response.json(repeat,{status:201});
      }
      if(o.method==='PATCH'&&+o.headers['Upload-Offset']===2*MB&&!failed){
        failed=true;await s.request(path,o);assert.equal(get('send').disabled,true);throw new TypeError('Injected lost response');
      }
      try{return Response.json(await s.request(path,o));}catch(e){return Response.json({error:e.message,offset:e.offset},{status:e.status||503});}
    }};
  get('language').value='ru';
  vm.runInNewContext(fs.readFileSync(require.resolve('../static/app.js'),'utf8'),context);
  for(let n=0;n<20&&get('upload').hidden;n++)await new Promise(r=>setTimeout(r,1));
  await new Promise(r=>setTimeout(r,5));
  // Only physical disk space is reported; there is no personal quota to show.
  assert.match(get('capacity').textContent,/места на сервере/);
  assert.doesNotMatch(get('capacity').textContent,/лимит/i);
  const actions=()=>get('jobs').children.flatMap(card=>card.children);
  const action=actions().find(x=>x.textContent==='Расшифровать заново');
  await action.onclick();
  assert.ok(action.disabled);
  assert.ok(actions().some(x=>x.textContent==='Повторная расшифровка'));
  assert.ok(actions().some(x=>x.textContent==='Скачать расшифровку'));
  assert.ok(!actions().some(x=>x.textContent==='Расшифровать заново'));
  get('file').files=[f.file];get('file').onchange();
  const pending=get('send').onclick();await get('send').onclick();await pending;
  assert.equal(get('send').textContent,'Запись загружена');assert.equal(get('send').disabled,true);
  assert.match(get('notice').textContent,/Запись загружена/);assert.deepEqual(s.bytes,f.bytes);
  await get('send').onclick();assert.equal(s.creates,1);assert.equal(s.queued,1);
  const historyText=get('jobs').children.flatMap(x=>x.children.map(c=>c.textContent)).join(' ');
  assert.doesNotMatch(historyText,/выберите этот же файл/);
});
