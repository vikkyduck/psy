'use strict';
const assert=require('node:assert/strict');
const {createHash}=require('node:crypto');
const {transferRecording}=require(process.env.PSY_TEST_APP||'../static/app.js');
(async()=>{
  const base='http://127.0.0.1:'+process.argv[2];
  const auth=await(await fetch(base+'/api/auth',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({initData:process.argv[3]})})).json();
  assert.ok(auth.session);
  const bytes=Buffer.alloc(26*1024*1024+917);for(let n=0;n<bytes.length;n++)bytes[n]=(n*11+(n>>>17))%251;
  const file=new File([bytes],'http-interruption-test.wav');
  const fp=createHash('sha256').update(bytes.subarray(0,65536)).digest('hex');
  const failures=new Set();let retries=0;
  const request=async(path,options={})=>{
    const offset=Number(options.headers?.['Upload-Offset']);
    if(options.method==='PATCH'&&offset===6*1024*1024&&!failures.has('before')){failures.add('before');throw new TypeError('Simulated offline before sending');}
    if(!options.method&&path==='/api/jobs'&&failures.has('midpoint')&&!failures.has('sync')){failures.add('sync');throw new TypeError('Simulated offline during reconciliation');}
    const headers={'X-Upload-Session':auth.session,...options.headers};
    if(typeof options.body==='string')headers['Content-Type']='application/json';
    const response=await fetch(base+path,{...options,headers});const data=await response.json();
    if(!response.ok)throw Object.assign(new Error(data.error),{status:response.status,offset:data.offset});
    let key=null;
    if(options.method==='POST'&&path==='/api/jobs')key='creation';
    if(options.method==='PATCH'&&offset===13*1024*1024)key='midpoint';
    if(path.endsWith('/complete'))key='completion';
    if(key&&!failures.has(key)){failures.add(key);throw new TypeError('Simulated lost response');}
    return data;
  };
  await transferRecording({file,language:'en',fingerprint:fp,request,wait:async()=>{retries++;}});
  const list=await request('/api/jobs');assert.equal(list.jobs.length,1);assert.equal(list.jobs[0].state,'queued');
  assert.equal(list.jobs[0].offset,bytes.length);assert.equal(retries,5);
  console.log(JSON.stringify({bytes:bytes.length,sha256:createHash('sha256').update(bytes).digest('hex'),recovered_failures:retries,jobs:1,state:'queued'}));
})().catch(e=>{console.error('HTTP recovery test failed:',e.name,e.status||'');process.exit(1);});
