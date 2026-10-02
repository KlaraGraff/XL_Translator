// Run: node agent/testing/settings-data-health-cold-start.cjs
// Requires Node >= 22.13. No native shell, HTTP server or real network is used.
// Executes the actual bootstrap/connection functions and full ApiClient source.
const fs = require('node:fs');
const path = require('node:path');
const repoRoot = path.resolve(__dirname, '../..');
const artifacts = path.resolve(repoRoot, process.env.PRODUCT_TRANSLATE_SELF_TEST_ARTIFACTS || '.runtime/self-tests/settings-data-health-cold-start');
fs.mkdirSync(artifacts, { recursive: true });
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {stripTypeScriptTypes} = require('node:module');
const settings = fs.readFileSync(path.join(repoRoot, 'ui/src/views/settings.ts'), 'utf8');
const api = fs.readFileSync(path.join(repoRoot, 'ui/src/api-client.ts'), 'utf8').replace(/^import .*;\n/gm, '').replace(/^export /gm, '');
function functionSource(name) {
  const start = settings.indexOf(`async function ${name}(`);
  const end = settings.indexOf('\n}\n', start) + 3;
  assert(start >= 0 && end > start);
  return settings.slice(start, end);
}
const code = `${api}\nconst client = new ApiClient(); let connectPromise = null; let mountToken = 1; let dataHealth = null; const bodyHost = null;
${functionSource('ensureConnected')}
${functionSource('bootstrap')}
${functionSource('refreshDataHealth')}
const refreshSettings = async () => {}; const refreshLanguages = async () => {}; const loadUpdateState = async () => {}; const loadAndRenderPage = async () => {};
function renderDataHealthBanner() { banners.push(dataHealth); }
globalThis.runBootstrap = bootstrap; globalThis.runRefresh = refreshDataHealth; globalThis.unmount = () => { mountToken += 1; };
`;
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() { let resolve; let reject; const promise = new Promise((a,b) => {resolve=a;reject=b;}); return {promise,resolve,reject}; }
function context(invoke, fetch) { const context={invoke,fetch,Headers,AbortController,TextDecoder,setTimeout,console,banners:[]}; vm.createContext(context); vm.runInContext(stripTypeScriptTypes(code),context);return context; }
async function main() {
  const handshake=deferred(), ping=deferred(), calls=[];
  const c=context(()=>handshake.promise,async(url,options)=>{calls.push({url,token:options.headers.get('X-Translator-Token')}); if(new URL(url).pathname === '/health')return ping.promise; return new Response(JSON.stringify({task_history:{state:'recreated',backup_path:'/backup'}}));});
  const boot=c.runBootstrap(1);
  await tick(); assert.equal(calls.length,0);
  handshake.resolve({port:43219,token:'isolated-token'});
  await tick(); assert.equal(calls.length,1); assert.equal(calls[0].url,'http://127.0.0.1:43219/health');
  ping.resolve(new Response('{}')); await boot; await tick();
  assert.equal(calls[1].url,'http://127.0.0.1:43219/api/data/health'); assert.equal(calls[1].token,'isolated-token'); assert.equal(c.banners[0].task_history.state,'recreated');
  const staleHandshake=deferred(), staleCalls=[];
  const stale=context(()=>staleHandshake.promise,async(url)=>{staleCalls.push(url);return new Response('{}');});
  const pending=stale.runRefresh(1); stale.unmount(); staleHandshake.resolve({port:43220,token:'stale'}); await pending;
  assert.equal(staleCalls.length,1); assert(staleCalls[0].endsWith('/health')); assert.equal(stale.banners.length,0);
  let failed=true; let attempts=0; const retryCalls=[];
  const retry=context(async()=>{attempts++;return {port:43221,token:'retry-token'};},async(url,options)=>{retryCalls.push({url,token:options.headers.get('X-Translator-Token')}); if(failed) {failed=false;throw new Error('not ready');}return new Response('{}');});
  await retry.runBootstrap(1); assert.equal(attempts,1); assert.equal(retryCalls.length,1);
  await retry.runRefresh(1); assert.equal(attempts,2); assert.equal(retryCalls.at(-1).url,'http://127.0.0.1:43221/api/data/health'); assert.equal(retryCalls.at(-1).token,'retry-token');
  fs.writeFileSync(path.join(artifacts, 'result.json'), JSON.stringify({ passed: true, checks: ['bootstrap waits for handshake', 'data health waits for successful sidecar health', 'sidecar URL and token supplied', 'stale mount guarded', 'connection failure permits retry'] }, null, 2) + '\n');
  console.log('PASS: real bootstrap and ApiClient wait for sidecar handshake and health; correct base URL/token; stale mount guarded; failed connection retries');
}
main().catch(error => {
  fs.writeFileSync(path.join(artifacts, 'result.json'), JSON.stringify({ passed: false, error: String(error.stack || error) }, null, 2) + '\n');
  console.error(error);
  process.exitCode = 1;
});
