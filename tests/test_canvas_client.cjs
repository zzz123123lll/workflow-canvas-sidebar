const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../client.js'), 'utf8');

async function testFailedSaveCannotStartRun() {
  const calls = [];
  const context = {
    stateRef: { current: { graph: { nodes: [{ id: 'audit' }], edges: [] }, run: null } },
    selectedRunRef: { current: '' }, projectRef: {current:null}, projectEpochRef: {current:0},
    setBusy() {}, setGraph() {}, setDirty() {}, setMsg() {}, setRun() {}, stopAtReview: false,
    startingRef: {}, startTriesRef: {}, seenRunIdRef: {}, runStartRef: {},
    askText() { return Promise.resolve('fixture-task'); }, canvasRevision: '', setCanvasRevision() {},
    postJson(url) { calls.push(url); return url === '/canvas' ? Promise.reject(new Error('save failed')) : Promise.resolve({ ok: true }); },
  };
  vm.runInNewContext(source.slice(source.indexOf('      function save() {'), source.indexOf('      function cancelRun() {')) + '\nrunWorkflow();', context);
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(calls, ['/canvas']);
}

async function testBodyErrorIsNotSuccess() {
  const start = source.indexOf('    function fetchJson(');
  const end = source.indexOf('    function postJson(', start);
  const context = { API: '/fixture', fetch: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: false, error: 'not ready' }) }) };
  vm.runInNewContext(source.slice(start, end), context);
  await assert.rejects(context.fetchJson('/request'), /not ready/);
}

async function testKnowledgeRequestsCarrySelectedTask() {
  const requests = [];
  const context = { stateRef: { current: { kbTask: 'task-A', run: { task_id: 'task-B' } } },
    kbRequestRef: { current: 0 }, kbReadRef: { current: 0 },
    setKbPath() {}, setKbContent() {}, setKbEdit() {}, setKbMeta() {}, setKbEntries() {}, setMsg() {},
    fetchJson(url) { requests.push(url); return Promise.resolve({ entries: [] }); } };
  const start = source.indexOf('      function knowledgeTask()');
  const end = source.indexOf('      function searchKb(', start);
  vm.runInNewContext(source.slice(start, end), context);
  assert.equal(context.knowledgeTask(), 'task-A');
  await context.loadKbEntries(context.knowledgeTask(), '橘子');
  assert.equal(requests.length, 1);
  const url = new URL(requests[0], 'http://fixture');
  assert.equal(url.searchParams.get('task_id'), 'task-A');
  assert.equal(url.searchParams.get('q'), '橘子');
  await context.loadKbEntries('', '');
  assert.equal(requests.length, 2, 'missing task may browse shared knowledge only');
  const sharedUrl = new URL(requests[1], 'http://fixture');
  assert.equal(sharedUrl.searchParams.get('scope'), 'shared');
  assert.equal(sharedUrl.searchParams.get('task_id'), '');
}

async function testLateKnowledgeResponseCannotReplaceNewPartition() {
  const pending = [], visible = [];
  const context = {
    kbRequestRef: { current: 0 }, kbReadRef: { current: 0 },
    setKbPath() {}, setKbContent() {}, setKbEdit() {}, setKbMeta() {}, setMsg() {},
    setKbEntries(value) { visible.push(value); },
    fetchJson() { return new Promise(resolve => pending.push(resolve)); },
  };
  const start = source.indexOf('      function loadKbEntries(');
  const end = source.indexOf('      function searchKb(', start);
  vm.runInNewContext(source.slice(start, end), context);
  const old = context.loadKbEntries('old-task', '');
  const latest = context.loadKbEntries('new-task', '');
  pending[1]({ entries: ['new-partition-result'] });
  await latest;
  pending[0]({ entries: ['old-partition-result'] });
  await old;
  assert.deepEqual(visible.at(-1), ['new-partition-result']);
}

function testHostRejectsUntrustedOrigins() {
  const host = fs.readFileSync(path.join(__dirname, '../index.js'), 'utf8');
  const start = host.indexOf('function trustedOrigin(');
  const end = host.indexOf('\nfunction acceptOrigin(', start);
  const context = { URL, url: 'http://127.0.0.1:8792/' };
  vm.runInNewContext(host.slice(start, end), context);
  for (const origin of ['https://evil.example', 'null', 'http://127.0.0.1.evil.example:8792', 'http://user@localhost:8792']) {
    assert.equal(context.trustedOrigin(origin), false);
  }
  assert.equal(context.trustedOrigin('http://127.0.0.1:8792'), true);
  assert.equal(context.trustedOrigin('http://127.0.0.1:9999'), false);
  assert.equal(context.trustedOrigin('dsh-app://app'), true);
  assert.equal(context.trustedOrigin(undefined), true);
}

async function testUnavailableExecutionServiceCannotFallBackToLegacyRunner() {
  const host = fs.readFileSync(path.join(__dirname, '../index.js'), 'utf8');
  const start = host.indexOf('async function proxyApi(');
  const end = host.indexOf('\nexport function apply(', start);
  const calls = [], response = {};
  let requestedRestart = false;
  const context = {
    acceptOrigin: () => true,
    server: { address: () => ({ port: 8792 }) },
    AbortSignal, Buffer, API_PORT: 8793, API_VERSION: '0.14.0',
    ensurePythonApi() { requestedRestart = true; },
    apiHealth() { calls.push('health'); return Promise.reject(new Error('offline')); }, matchesApi: () => false,
  };
  vm.runInNewContext(host.slice(start, end), context);
  await context.proxyApi({method:'GET',url:'/dsh-workflow/api/wf/status',headers:{}}, {
    writeHead(code) { response.code = code; }, end(body) { response.body = body; },
  });
  assert.equal(response.code,503);
  assert.equal(calls.length,1, 'must not call the legacy loopback workflow runner');
  assert.equal(requestedRestart,true);
}

async function testArtifactsLoadIntoPanel() {
  const calls = [], states = [];
  const context = {
    taskCtxRef: { current: { runId: 'run-1', nodeId: 'n1' } },
    artifactSeqRef: { current: 0 },
    sameTaskContext(c) {
      const now = context.taskCtxRef.current;
      return !!c && !!now && now.runId === c.runId && now.nodeId === c.nodeId;
    },
    setArtifactRows(rows) { context.rows = rows; },
    setArtifactState(value) { states.push(value); },
    setArtifactError() {},
    fetchJson(url) { calls.push(url); return Promise.resolve({ artifacts: [{ path: 'episodes/a.md', kind: 'text' }] }); },
  };
  const start = source.indexOf('      function loadArtifacts(');
  const end = source.indexOf('      function openArtifact(', start);
  vm.runInNewContext(source.slice(start, end), context);
  await context.loadArtifacts('run-1', 'n1');
  assert.equal(context.rows.length, 1);
  assert.deepEqual(states, ['loading', 'ready']);
  assert.match(calls[0], /run_id=run-1/);
  assert.match(calls[0], /node=n1/);
}

async function testLateArtifactResponseCannotReplaceNewNode() {
  const pending = [], visible = [];
  const context = {
    taskCtxRef: { current: { runId: 'run-A', nodeId: 'n1' } },
    artifactSeqRef: { current: 0 },
    sameTaskContext(c) {
      const now = context.taskCtxRef.current;
      return !!c && !!now && now.runId === c.runId && now.nodeId === c.nodeId;
    },
    setArtifactRows(rows) { visible.push(rows); },
    setArtifactState() {}, setArtifactError() {},
    fetchJson() { return new Promise(resolve => pending.push(resolve)); },
  };
  const start = source.indexOf('      function loadArtifacts(');
  const end = source.indexOf('      function openArtifact(', start);
  vm.runInNewContext(source.slice(start, end), context);
  const first = context.loadArtifacts('run-A', 'n1');
  context.taskCtxRef.current = { runId: 'run-B', nodeId: 'n2' };
  const second = context.loadArtifacts('run-B', 'n2');
  pending[1]({ artifacts: [{ path: 'B' }] });
  await second;
  pending[0]({ artifacts: [{ path: 'A' }] });
  await first;
  assert.equal(visible.at(-1)[0].path, 'B');
}

async function testMediaArtifactUsesStreamingUrl() {
  const modals = [];
  const context = {
    taskCtxRef: { current: { runId: 'run-1', nodeId: 'n1' } },
    stateRef: { current: { run: { run_id: 'run-2' } } },
    API: '/dsh-workflow/api', ws: {generation:1},
    sameTaskContext() { return true; },
    setArtifactModal(value) { modals.push(value); },
    setBusy() {}, setMsg() {},
  };
  const start = source.indexOf('      function openArtifact(');
  const end = source.indexOf('      function approveVisual(', start);
  vm.runInNewContext(source.slice(start, end), context);
  context.openArtifact({ path: 'episodes/v.mp4', kind: 'video', bytes: 10 });
  assert.equal(modals.length, 1);
  assert.match(modals[0].url, /\/wf\/raw\?run_id=run-1&path=episodes%2Fv\.mp4/);
  assert.doesNotMatch(modals[0].url, /run-2/);
  assert.match(modals[0].download, /download=1/);
}

async function testLateArtifactCannotReopenClosedDrawer() {
  const modals = [];
  let resolveFetch = null;
  const context = {
    taskCtxRef: { current: { runId: 'run-A', nodeId: 'n1' } },
    API: '/dsh-workflow/api', ws: {generation:1},
    sameTaskContext(c) {
      const now = context.taskCtxRef.current;
      return !!c && !!now && now.runId === c.runId && now.nodeId === c.nodeId;
    },
    setArtifactModal(value) { modals.push(value); },
    setBusy() {}, setMsg() {},
    fetchJson() { return new Promise(resolve => { resolveFetch = resolve; }); },
  };
  const start = source.indexOf('      function openArtifact(');
  const end = source.indexOf('      function approveVisual(', start);
  vm.runInNewContext(source.slice(start, end), context);
  context.openArtifact({ path: 'episodes/a.md', kind: 'text' });
  context.taskCtxRef.current = null; // drawer closed while the read was in flight
  resolveFetch({ kind: 'text', text: 'late' });
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(modals.length, 0);
}

async function testWorkspaceUsesSelectedRun() {
  const calls = [], seen = [];
  const context = {
    taskCtxRef: { current: { runId: 'run-A', nodeId: 'n1' } },
    wsSeqRef: { current: 0 },
    sameTaskContext(c) {
      const now = context.taskCtxRef.current;
      return !!c && !!now && now.runId === c.runId && now.nodeId === c.nodeId;
    },
    setWs(value) { seen.push(value); },
    setWsErr() {}, setWsLoading() {}, setTaskInfo() {}, rememberProjectView() {},
    fetchJson(url) {
      calls.push(url);
      return Promise.resolve({ node: { id: 'n1', status: 'awaiting_input' }, run: { run_id: 'run-A' },
        messages: [{ role: 'assistant', kind: 'question', text: 'real question' }] });
    },
  };
  const start = source.indexOf('      function loadWorkspace(');
  const end = source.indexOf('      function openWorkspace(', start);
  vm.runInNewContext(source.slice(start, end), context);
  await context.loadWorkspace('run-A', 'n1');
  assert.match(calls[0], /run_id=run-A/);
  assert.match(calls[0], /node=n1/);
  assert.equal(seen.at(-1).messages[0].text, 'real question');
}

async function testCustomModuleFormSavesItsDeclaredContract() {
  const calls = [], added = [];
  const context = { busy: false,
    moduleForm: { name: 'module', instructions: 'do work', inputs: 'source/a.md\n source/b.json ',
      outputs: 'episodes/{stem}-result.md\nepisodes/{stem}-result.json', timeout: '900', checklist: 'Read inputs\nWrite outputs' },
    setBusy() {}, setModuleError() {}, setModules() {}, setSearch() {}, setModuleForm() {}, setHelpModule() {}, setMsg() {},
    addFromPalette(value) { added.push(value); },
    postJson(url, body) { calls.push({url, body: JSON.parse(JSON.stringify(body))}); return Promise.resolve({module:{id:'saved'}, modules:[]}); }
  };
  const start = source.indexOf('      function createLibraryModule(');
  const end = source.indexOf('      async function deleteLibraryModule(', start);
  vm.runInNewContext(source.slice(start, end), context);
  context.createLibraryModule(false);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(calls[0].url, '/modules/create');
  assert.deepEqual(calls[0].body.requires, ['source/a.md','source/b.json']);
  assert.deepEqual(calls[0].body.artifacts, ['episodes/{stem}-result.md','episodes/{stem}-result.json']);
  assert.equal(calls[0].body.timeout_seconds, '900');
  context.moduleForm.editingId = 'existing';
  context.createLibraryModule(false);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(calls[1].url, '/modules/update');
  assert.equal(calls[1].body.id, 'existing');
  assert.equal(added.length, 0, 'editing must not create another canvas instance');
}

Promise.resolve().then(testFailedSaveCannotStartRun).then(testBodyErrorIsNotSuccess)
  .then(testKnowledgeRequestsCarrySelectedTask).then(testLateKnowledgeResponseCannotReplaceNewPartition)
  .then(testHostRejectsUntrustedOrigins).then(testUnavailableExecutionServiceCannotFallBackToLegacyRunner)
  .then(testArtifactsLoadIntoPanel).then(testLateArtifactResponseCannotReplaceNewNode)
  .then(testMediaArtifactUsesStreamingUrl).then(testLateArtifactCannotReopenClosedDrawer)
  .then(testWorkspaceUsesSelectedRun)
  .then(testCustomModuleFormSavesItsDeclaredContract)
  .then(() => console.log('12 client behavior tests passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });


