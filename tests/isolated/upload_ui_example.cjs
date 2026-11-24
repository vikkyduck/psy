'use strict';
// Execute the deployed DOM click handler, rather than a reimplementation of its upload loop.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {createHash, webcrypto} = require('node:crypto');
if (typeof global.File === 'undefined') global.File = require('node:buffer').File;

class Element {
  constructor() { this.hidden = false; this.disabled = false; this.value = ''; this.textContent = ''; this.children = []; this.files = []; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; }
  addEventListener() {}
  click() { if (this.onclick) return this.onclick(); }
}

async function main() {
  const [base, initData, source, appPath] = process.argv.slice(2);
  const origin = new URL(base);
  assert.equal(origin.hostname, '127.0.0.1');
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  get('upload').hidden = true;
  get('language').value = 'en';
  global.document = { getElementById: get, createElement: () => new Element(), visibilityState: 'visible', addEventListener() {} };
  global.window = { Telegram: {WebApp: {initData, ready() {}, expand() {}, enableClosingConfirmation() {}, disableClosingConfirmation() {}}}, confirm: () => false };
  global.location = {search: ''};
  Object.defineProperty(global, 'navigator', {value: {}, configurable: true});
  if (!global.crypto) global.crypto = webcrypto;
  const actualFetch = global.fetch;
  let lostReply = false;
  let chunkRequests = 0;
  let completeRequests = 0;
  global.fetch = async (path, options = {}) => {
    const url = new URL(path, base);
    assert.equal(url.origin, origin.origin, 'Any external network request is forbidden');
    const response = await actualFetch(url, options);
    if (options.method === 'PATCH') {
      chunkRequests++;
      // The server saved this part, but its reply was lost. No second UI tap is allowed.
      if (!lostReply && Number(options.headers?.['Upload-Offset']) >= 13 * 1024 ** 2) {
        lostReply = true;
        await response.arrayBuffer();
        throw new TypeError('Synthetic lost chunk acknowledgement');
      }
    }
    if (url.pathname.endsWith('/complete')) completeRequests++;
    return response;
  };
  // Background UI refresh intervals should not keep the test process alive.
  global.setInterval = () => 0;
  require(appPath);
  const end = Date.now() + 15000;
  while (get('upload').hidden && Date.now() < end) await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(get('upload').hidden, false, 'Actual WebApp authorization failed');
  assert.equal(get('destination').textContent, 'Группа');
  const bytes = fs.readFileSync(source);
  assert.ok(bytes.length > 20 * 1024 ** 2);
  get('file').files = [new File([bytes], 'synthetic-large-recording.wav', {type: 'audio/wav'})];
  get('file').onchange();
  assert.equal(get('send').disabled, false);
  const taps = 1;
  await get('send').onclick();
  assert.equal(get('send').textContent, 'Запись загружена', get('notice').textContent);
  assert.equal(get('send').disabled, true);
  assert.equal(get('progress').value, 100);
  assert.equal(lostReply, true);
  assert.equal(completeRequests, 1);
  assert.ok(chunkRequests > 20);
  console.log(JSON.stringify({ui_send_taps: taps, actual_dom_handler: true, destination: 'group',
    bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex'),
    chunk_requests: chunkRequests, complete_requests: completeRequests,
    recovered_lost_chunk_reply: lostReply, progress_percent: get('progress').value,
    app_sha256: createHash('sha256').update(fs.readFileSync(appPath)).digest('hex')}));
}
main().catch(error => { console.error('isolated UI example failed:', error.name, error.message); process.exit(1); });
