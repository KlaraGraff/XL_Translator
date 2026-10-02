import assert from 'node:assert/strict';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { stripTypeScriptTypes } from 'node:module';
import vm from 'node:vm';
const artifacts = '.runtime/self-tests/project-audit-2026-10-02/tm-races';
await mkdir(artifacts, { recursive: true });
const raw = await readFile('ui/src/views/library.ts', 'utf8');
const code = stripTypeScriptTypes(raw.replace(/^import[\s\S]*?from ["'][^"']+["'];\s*/gm, '').replace(/^export /gm, ''));
const checks = [];
const assertions = [];
function covered(name) { assertions.push(name); }
function harness() {
  const requests = [], toasts = [], layouts = [];
  const client = { request(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); } };
  const c = vm.createContext({ console, Error, showToast: x => toasts.push(x), setTopbar() {}, closeMenu() {}, hideHint() {}, closeLanguagePopover() {}, document: { querySelectorAll: () => [] }, window: { clearTimeout() {} } });
  vm.runInContext(code, c);
  c.client = client; c.layouts = layouts;
  vm.runInContext(`getClient = async () => client; mounted = true; mountGeneration = 1; tmQueryPending = false;
    renderTable = renderTopbarStatus = updateSelectionUi = rebuildToolbar = renderStatsRow = renderStateRow = () => {};
    renderLoadingPlaceholder = () => {}; buildLayout = container => layouts.push(container);
    refreshCleanBadge = async () => {};`, c);
  return { c, requests, toasts, layouts, run: x => vm.runInContext(x, c), state: () => JSON.parse(vm.runInContext('JSON.stringify({keyword,page,entries,selected:[...selectedIds],sourceLang,targetLang})', c)) };
}
const tick = async () => { for (let i=0;i<8;i++) await Promise.resolve(); };
const payload = (id, total=100) => ({ entries: [{ id }], total, stats: {} });
async function test(name, fn) { await fn(); checks.push({ name, passed: true }); }
await test('slow A fast B, old failure silent, latest failure restores confirmed query', async () => {
  const h=harness();
  const a=h.run('applyTmQueryChange(() => {keyword="alpha"})'); await tick();
  const b=h.run('applyTmQueryChange(() => {keyword="beta"})'); await tick();
  h.requests[1].resolve(payload(2)); await b;
  h.requests[0].resolve(payload(1)); await a;
  assert.equal(h.state().entries[0].id,2); assert.equal(h.state().keyword,'beta'); covered('A success after B cannot overwrite B entries or keyword');
  const old=h.run('applyTmQueryChange(() => {keyword="old"})'); await tick();
  const next=h.run('applyTmQueryChange(() => {keyword="next"})'); await tick();
  h.requests[3].resolve(payload(3)); await next; h.requests[2].reject(new Error('old')); await old;
  assert.equal(h.state().keyword,'next'); assert.equal(h.toasts.length,0); covered('stale query failure produces no rollback or toast');
  const fail=h.run('applyTmQueryChange(() => {keyword="failed"})'); await tick(); h.requests[4].reject(new Error('latest')); await fail;
  assert.equal(h.state().keyword,'next'); assert.equal(h.toasts.length,1); covered('latest failure restores last confirmed query, not intermediate pending query');
});
await test('rapid pagination and page fallback discard old result', async () => {
 const h=harness(); const a=h.run('applyTmQueryChange(() => {page=2})'); await tick(); const b=h.run('applyTmQueryChange(() => {page=3})'); await tick();
 h.requests[1].resolve(payload(3,25)); await tick(); assert.match(h.requests[2].url,/page=1&/);
 h.requests[0].resolve(payload(2)); await a; h.requests[2].resolve(payload(1,25)); await b;
 assert.equal(h.state().page,1); assert.equal(h.state().entries[0].id,1); covered('page 2 delayed behind page 3 fallback cannot overwrite page 1');
});
await test('select all cannot cross query or language pair, latest switch wins', async () => {
 const h=harness(); h.run('total=20'); const all=h.run('handleSelectAllTm()'); await tick();
 const search=h.run('applyTmQueryChange(() => {keyword="new"})'); await tick(); h.requests[1].resolve(payload(9)); await search; h.requests[0].resolve(payload(4)); await all; assert.deepEqual(h.state().selected,[]); covered('cross-query select-all response cannot populate selection');
 const all2=h.run('handleSelectAllTm()'); await tick(); const a=h.run('saveLangPair("zh","fr")'); await tick(); const b=h.run('saveLangPair("zh","de")'); await tick();
 assert.equal(h.requests.length,4); covered('B language save waits while A PUT is pending');
 h.requests[3].resolve({}); await a; await tick();
 assert.equal(JSON.parse(h.requests[4].options.body).tm_target_lang,'de');
 h.requests[4].resolve({}); await tick(); h.requests[5].resolve(payload(10)); await tick(); h.requests[6].resolve({conflicts:[]}); await b;
 h.requests[2].resolve(payload(5)); await all2;
 const read=h.run('refreshLanguagePairs()'); await tick();
 const persisted=JSON.parse(h.requests[4].options.body);
 h.requests[7].resolve({source_options:[{code:'zh'}],target_options:[{code:'en'},{code:'fr'},{code:'de'}],selected:{source_lang:persisted.tm_source_lang,target_lang:persisted.tm_target_lang}}); await read;
 covered('next language-pair read observes B as final persisted selection');
 assert.equal(h.state().targetLang,'de'); covered('language pair writes A then B keep final B label and entries'); assert.equal(h.state().entries[0].id,10); assert.deepEqual(h.state().selected,[]);
});
await test('unmount remount ignores old initial load and old requests', async () => {
 const h=harness(); const old=h.run('loadLibrary("old",null)'); await tick();
 h.run('unmount(); mount("new", {})'.replace('mount("new", {})','mount({style:{},name:"new"}, {})')); await tick();
 const lang={ source_options:[{code:'zh'}], target_options:[{code:'en'}],selected:{source_lang:'zh',target_lang:'en'}};
 h.requests[1].resolve(lang); await tick(); h.requests[2].resolve(payload(7)); h.requests[3].resolve({conflicts:[]}); await tick();
 h.requests[0].resolve(lang); await old; assert.equal(h.layouts.length,1); assert.equal(h.layouts[0].name,'new'); covered('old initial language response cannot build old container after remount'); assert.equal(h.state().entries[0].id,7);
 const pending=h.run('refreshTm()'); await tick(); h.run('unmount(); mounted=true; mountGeneration++;'); h.requests[4].resolve(payload(1)); await pending; assert.equal(h.state().entries[0].id,7);
});
await test('obsolete queued save is skipped without skipping the latest intent', async () => {
 const h=harness(); const a=h.run('saveLangPair("zh","fr")'); await tick();
 const b=h.run('saveLangPair("zh","de")'); const c=h.run('saveLangPair("zh","es")'); await tick();
 assert.equal(h.requests.length,1); h.requests[0].resolve({}); await a; await b; await tick();
 assert.equal(h.requests.length,2); assert.equal(JSON.parse(h.requests[1].options.body).tm_target_lang,'es');
 h.requests[1].resolve({}); await tick(); h.requests[2].resolve(payload(33)); await tick(); h.requests[3].resolve({conflicts:[]}); await c;
 assert.equal(h.state().targetLang,'es'); assert.equal(h.state().entries[0].id,33);
 covered('queued B skips its obsolete write while latest C still persists and loads');
});
await test('pending query actions are blocked and language failures restore confirmed scope', async () => {
 const h=harness(); h.run('entries=[{id:21}]; total=20; selectedIds.add(21)');
 const pending=h.run('applyTmQueryChange(() => {keyword="pending"})'); await tick();
 const count=h.requests.length; await h.run('handleSelectAllTm()'); await h.run('tmBulkPin(true)'); await h.run('tmPin(21,true)'); h.run('confirmBulkDelete(); openDeleteModal(entries[0]); openAddEditModal(entries[0])');
 assert.equal(h.requests.length,count); covered('pending query blocks select-all, bulk pin/delete, row pin/delete/edit');
 h.requests[0].reject(new Error('search')); await pending; assert.deepEqual(h.state().selected,[21]);
 const pair=h.run('saveLangPair("zh","fr")'); await tick(); h.requests[1].reject(new Error('persist')); await pair;
 assert.equal(h.state().targetLang,'en'); assert.equal(h.state().entries[0].id,21); assert.deepEqual(h.state().selected,[21]); assert.equal(h.run('tmQueryReady()'),true); covered('language persistence rejection restores confirmed pair, entries, selection and ready state');
 const pair2=h.run('saveLangPair("zh","de")'); await tick(); h.requests[2].resolve({}); await tick(); h.requests[3].reject(new Error('entries')); await pair2;
 assert.equal(h.state().targetLang,'en'); assert.equal(h.state().entries[0].id,21); assert.deepEqual(h.state().selected,[21]); covered('language entries rejection restores same confirmed display scope');
});
await writeFile(`${artifacts}/results.json`,JSON.stringify({ checks, assertions },null,2));
console.log(JSON.stringify({ checks, assertions },null,2));
