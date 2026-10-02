const fs=require('fs'),vm=require('vm'),assert=require('assert');
const {stripTypeScriptTypes}=require('module');
let source=fs.readFileSync('ui/src/update-controller.ts','utf8');
source=source.replace(/import[\s\S]*?from "[^"\n]+";/g,'').replace(/export /g,'');
const code=`const {ApiClient}=require('./api-client');const {openModal,showToast}=require('./components');const {setSettingsAlert}=require('./shell');const {restartApp,resolveUpdate}=require('./update-service');`+stripTypeScriptTypes(source)+`;exports.requestRestart=requestRestart;exports.requestUpdateInstall=requestUpdateInstall;exports.updateSnapshot=updateSnapshot;`;
async function scenario(counts, windows=false) {
 let restarted=0,installed=0,modals=0;const toasts=[];
 class ApiClient {async connect(){} async listTasks(){const n=counts.shift();if(n instanceof Error)throw n;return {active:[],active_work_count:n};}}
 const sandbox={exports:{},console,require(name){if(name==='./api-client')return {ApiClient};if(name==='./components')return {openModal(o){modals++;o.actions[0].onClick();},showToast(o){toasts.push(o.message);}};if(name==='./shell')return {setSettingsAlert(){}};return {restartApp:async()=>{restarted++},resolveUpdate:async()=>({version:'10',download:async()=>{},install:async()=>{installed++},close:async()=>{}})}}};
 vm.runInNewContext(code,sandbox);const api=sandbox.exports;
 if(windows){api.updateSnapshot().env={installBehavior:'installer_restart'};await api.requestUpdateInstall();}else {api.updateSnapshot().flow.phase='ready';await api.requestRestart();}
 return {restarted,installed,modals,toasts,phase:api.updateSnapshot().flow.phase,flow:api.updateSnapshot().flow};
}
(async()=>{
 let r=await scenario([new Error('offline')]);assert.equal(r.restarted,0);assert.equal(r.phase,'ready');assert.match(r.toasts[0],/无法确认/);
 r=await scenario([1]);assert.equal(r.modals,1);assert.equal(r.restarted,1);
 r=await scenario([0]);assert.equal(r.modals,0);assert.equal(r.restarted,1);
 r=await scenario([0,1],true);assert.equal(r.modals,2);assert.equal(r.installed,1);
 r=await scenario([0,new Error('offline')],true);assert.equal(r.installed,0);assert.equal(r.flow.failureTitle,'暂时无法安装');assert.match(r.flow.message,/无法确认/);assert.doesNotMatch(r.flow.message,/没有下载完/);
 console.log('restart failure, active work, idle, Windows late work and failed recheck passed');
})();
