const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../client.js'),'utf8');
const pure={};
vm.runInNewContext(source.slice(source.indexOf('    function workspaceMessageLabel('),source.indexOf('    function Icon(')),pure);
assert.match(pure.workspaceMessageLabel({role:'assistant',kind:'summary'}),/执行摘要/);
assert.match(pure.workspaceMessageLabel({role:'assistant',kind:'legacy_summary'}),/历史执行摘要/);
assert.match(pure.workspaceMessageLabel({role:'assistant',kind:'question'}),/提问/);
assert.equal(pure.workspaceSessionId({session_id:'real-sdk-session',node:{module:'ai'}}),'real-sdk-session');
assert.equal(pure.workspaceSessionId({node:{session_id:'old-session'}}),'old-session');
assert.match(pure.knowledgeParseLabel({parse_status:'unsupported'}),/不支持/);
assert.match(pure.knowledgeParseLabel({parse_status:'parsed',usable:true}),/可用于执行/);

async function testRevisionUsesFixedContext(){
 const calls=[];
 const c={taskCtxRef:{current:{runId:'old-run',nodeId:'old-node'}},wsText:'请增加一个例子',ws:{node:{status:'passed'}},busy:false,
 setBusy(){},setMsg(){},setWsText(){},sameTaskContext(){return true;},loadWorkspace(){},openWorkspace(){},workspaceMutationRef:{current:null},
 postRunCommand(p,b){calls.push({p,b});return Promise.resolve({run_id:'old-run'});},
 window:{crypto:{randomUUID:()=> 'revision-request-1'}},Date};
 vm.runInNewContext(source.slice(source.indexOf('      function reviseWorkspace('),source.indexOf('      function openTaskForCurrent(')),c);
 await c.reviseWorkspace();await new Promise(r=>setTimeout(r,0));
 assert.equal(calls[0].p,'/wf/revise');assert.equal(calls[0].b.run_id,'old-run');assert.equal(calls[0].b.node,'old-node');
 assert.equal(calls[0].b.request_id,'revision-request-1');
}
testRevisionUsesFixedContext().then(()=>console.log('8 product contracts passed')).catch(e=>{console.error(e);process.exitCode=1;});
