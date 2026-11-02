/**
 * dsh-workflow-canvas-sidebar
 *
 * Host half:
 *  - serves the workflow canvas HTML on a loopback origin (the right-sidebar
 *    Browser rejects the DSH application origin, so it gets its own port);
 *  - exposes the editor API (/api/workflow, /api/layout, /api/status) so the
 *    canvas can be edited without any extra process;
 *  - injects the effective URL into the page for the client half to open.
 */
import { createServer } from 'node:http'
import { readJson as readHttpJson, streamResponse } from './loopback-http.js'
import { readFileSync, writeFileSync, existsSync, mkdirSync, copyFileSync, cpSync, openSync, closeSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { execFile, spawn } from 'node:child_process'
import { homedir } from 'node:os'
import { randomBytes, randomUUID } from 'node:crypto'
import { bindingFile, readBinding, writeBinding, claimBinding, observeEvents, publicEvents, publicResult, isExecuting, continueChild } from './host-sessions.js'

// 本文件是 ES module（package.json "type": "module"），Node 不再提供 __dirname。
// 用 import.meta.url 还原，行为与 CommonJS 下的 __dirname 一致。
const __dirname = dirname(fileURLToPath(import.meta.url))

export const name = 'workflow-canvas-sidebar'

const SERVER_DIR = join(__dirname, 'server')
const BUNDLED_PY = join(__dirname, 'runtime', 'python', 'python.exe')
const INSTALLED = existsSync(join(SERVER_DIR, 'canvas_editor_server.py'))
let PLUGIN_CONFIG = {}
try { PLUGIN_CONFIG = JSON.parse(readFileSync(join(__dirname, 'canvas-home.json'), 'utf8')) } catch { PLUGIN_CONFIG = {} }
const BASE = process.env.CANVAS_HOME || PLUGIN_CONFIG.home || join(homedir(), '.dsh-workflow-canvas')
const CODE_CWD = SERVER_DIR
const CANVAS = join(INSTALLED ? SERVER_DIR : BASE, 'workflow-canvas.html')
const WORKFLOW = join(BASE, 'kb', 'workflow.json')
const LAYOUT = join(BASE, 'kb', 'workflow-canvas-layout.json')
const LIBRARY = join(BASE, 'kb', 'spec-library.json')
const MODULES = join(BASE, 'kb', 'modules.json')
const CANVAS_GRAPH = join(BASE, 'kb', 'canvas-workflow.json')
const RUNS = join(BASE, '.runs')
const SERVER = join(CODE_CWD, 'canvas_editor_server.py')
const WF = join(CODE_CWD, 'wf.py')
const PY = process.env.CANVAS_PYTHON || PLUGIN_CONFIG.python || (INSTALLED
  ? (existsSync(BUNDLED_PY) ? BUNDLED_PY
     : (existsSync(join(BASE, 'venv', 'Scripts', 'python.exe')) ? join(BASE, 'venv', 'Scripts', 'python.exe') : 'python'))
  : 'python')
const CHILD_ENV = Object.assign({}, process.env, { CANVAS_HOME: BASE })

function bootstrapHome() {
  if (!INSTALLED) return
  mkdirSync(BASE, { recursive: true })
  for (const directory of ['kb', 'episodes', 'artifacts', '.runs']) {
    mkdirSync(join(BASE, directory), { recursive: true })
  }
  const seedKb = join(__dirname, 'seed', 'kb')
  if (!existsSync(join(BASE, 'kb', 'modules.json')) && existsSync(seedKb)) {
    cpSync(seedKb, join(BASE, 'kb'), { recursive: true })
  }
}
bootstrapHome()
const PREFERRED = [8792, 8794, 8795, 8796]
const GLOBAL = '__DSH_WORKFLOW_CANVAS_URL__'
const BODY_LIMIT = 2 * 1024 * 1024
let API_PORT = Number(process.env.CANVAS_PORT || PLUGIN_CONFIG.port || 8793)
const API_VERSION = '0.17.0-rc.3'
const HOST_TOKEN = randomBytes(18).toString('hex')
const BUILD_INFO = readJson(join(__dirname, 'build-info.json'), { build_id: 'development', schema: 2 })
const INSTANCE_FILE = join(RUNS, 'api-instance.json')
const PREVIOUS_INSTANCE = readJson(INSTANCE_FILE, {})
let API_SECRET = PREVIOUS_INSTANCE.token || randomBytes(32).toString('hex')

let server = null
let url = null
let hostServices = null
let pluginCtx = null
const orchestrators = new Map()

function cors(res) {
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type')
}

function trustedOrigin(origin) {
  if (origin == null) return true
  if (origin === 'dsh-app://app') return true
  try {
    const value = new URL(origin)
    return value.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(value.hostname)
      && Boolean(value.port) && !value.username && !value.password
      && value.pathname === '/' && !value.search && !value.hash && Boolean(url) && value.origin === new URL(url).origin
  } catch { return false }
}

function acceptOrigin(req, res) {
  const origin = req.headers.origin
  if (!trustedOrigin(origin)) {
    sendJson(res, 403, { ok: false, error: '不接受来自外部网页的请求' })
    return false
  }
  if (origin) {
    res.setHeader('Access-Control-Allow-Origin', origin)
    res.setHeader('Vary', 'Origin')
  }
  return true
}

function sendJson(res, code, obj) {
  const body = Buffer.from(JSON.stringify(obj), 'utf8')
  cors(res)
  res.writeHead(code, { 'content-type': 'application/json; charset=utf-8', 'content-length': body.length })
  res.end(body)
}

function readJson(path, fallback) {
  try { return JSON.parse(readFileSync(path, 'utf8')) } catch { return fallback }
}

function runPyRaw(args, timeout = 300000) {
  return new Promise((resolve) => {
    execFile(PY, args, { cwd: CODE_CWD, env: CHILD_ENV, timeout, encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 },
      (err, stdout, stderr) => resolve({ ok: !err, stdout: stdout || '', stderr: stderr || '' }))
  })
}

function runPy(args, timeout = 120000) {
  return new Promise((resolve) => {
    execFile(PY, args, { cwd: CODE_CWD, env: CHILD_ENV, timeout, encoding: 'utf8' }, (err, stdout, stderr) => {
      if (err) return resolve({ ok: false, error: String(stderr || err.message || err) })
      try { resolve({ ok: true, data: JSON.parse(stdout) }) } catch (e) { resolve({ ok: false, error: String(e) }) }
    })
  })
}

function knownGates() {
  return runPy(['workflow.py', '--gates']).then((r) => {
    if (!r.ok || !Array.isArray(r.data)) return {}
    const out = {}
    for (const g of r.data) out[g.id] = g.label || g.id
    return out
  })
}

function validateLibrary(library) {
  if (!library || typeof library !== 'object' || !Array.isArray(library.blocks) || library.blocks.length === 0) {
    return 'library.blocks 必须是非空数组'
  }
  for (const b of library.blocks) {
    if (!b || !b.id || !Array.isArray(b.templates) || b.templates.length === 0) return '每个区块必须有 id 和非空 templates'
    const ids = new Set()
    for (const t of b.templates) {
      if (!t || !t.id || !t.name || !t.content) return `区块 ${b.id} 有模板缺 id/name/content`
      if (ids.has(t.id)) return `模板 id 重复：${t.id}`
      ids.add(t.id)
    }
  }
  return null
}

function latestRunState() {
  try {
    const rid = readFileSync(join(RUNS, 'LATEST'), 'utf8').trim()
    return JSON.parse(readFileSync(join(RUNS, rid, 'state.json'), 'utf8'))
  } catch { return null }
}

function runWf(args) {
  return new Promise((resolve) => {
    execFile(PY, [WF].concat(args), { cwd: CODE_CWD, env: CHILD_ENV, timeout: 60000, encoding: 'utf8' }, (err, stdout, stderr) => {
      resolve({ ok: !err, output: String(stdout || stderr || (err && err.message) || '') })
    })
  })
}

function spawnWf(args) {
  try {
    mkdirSync(RUNS, { recursive: true })
    const fd = openSync(join(RUNS, 'wf-last.log'), 'a')
    const child = spawn(PY, [WF].concat(args), { cwd: CODE_CWD, detached: true, windowsHide: true, stdio: ['ignore', fd, fd] })
    child.unref()
    closeSync(fd)
    return true
  } catch { return false }
}

function runJson(args, timeout = 120000) {
  return new Promise((resolve) => {
    execFile(PY, args, { cwd: CODE_CWD, env: CHILD_ENV, timeout, encoding: 'utf8' }, (err, stdout) => {
      try { resolve(JSON.parse(String(stdout || '{}'))) } catch { resolve({}) }
    })
  })
}

async function wfHealth() {
  const run = latestRunState()
  const bili = await runJson(['bin/bili.exe', 'auth', 'status'], 60000)
  const logged = Boolean((bili.data || {}).logged_in)
  let publish = null
  if (run && run.episode) publish = await runJson(['wf.py', 'rehearse', run.episode], 120000)
  return {
    run: run ? { id: run.run_id, status: run.status, episode: run.episode } : null,
    bili: { logged_in: logged, user: ((bili.data || {}).user || {}).name || '', error: ((bili.error || {}).message) || '' },
    publish: publish ? { ok: publish.ok, problems: publish.problems || [], command_file: publish.command_file } : null,
  }
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    req.on('data', (c) => {
      size += c.length
      if (size > BODY_LIMIT) { reject(new Error('body too large')); req.destroy(); return }
      chunks.push(c)
    })
    req.on('end', () => {
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')) } catch (e) { reject(e) }
    })
    req.on('error', reject)
  })
}

function validateWorkflow(wf) {
  if (!wf || typeof wf !== 'object' || !Array.isArray(wf.stages) || wf.stages.length === 0) {
    return 'workflow.stages 必须是非空数组'
  }
  const ids = new Set()
  for (let i = 0; i < wf.stages.length; i++) {
    const st = wf.stages[i]
    if (!st || typeof st !== 'object' || !st.id) return `stage #${i} 缺 id`
    if (ids.has(st.id)) return `stage id 重复：${st.id}`
    ids.add(st.id)
    if (!Array.isArray(st.requirements)) st.requirements = []
    if (!Array.isArray(st.commands)) st.commands = []
  }
  return null
}

function saveWorkflow(wf) {
  const err = validateWorkflow(wf)
  if (err) return { code: 400, body: { ok: false, error: err } }
  const ts = new Date().toISOString().replace(/[:T]/g, '-').slice(0, 19)
  const backup = join(BASE, 'kb', `workflow.backup-${ts}.json`)
  try {
    mkdirSync(join(BASE, 'kb'), { recursive: true })
    if (existsSync(WORKFLOW)) copyFileSync(WORKFLOW, backup)
  } catch { /* backup is best-effort */ }
  wf.updated = new Date().toISOString().slice(0, 10)
  writeFileSync(WORKFLOW, JSON.stringify(wf, null, 2), 'utf8')
  return { code: 200, body: { ok: true, backup, stages: wf.stages.length } }
}

function workflowStatus() {
  return new Promise((resolve) => {
    execFile(PY, ['workflow.py', 'status', '--json'], { cwd: CODE_CWD, env: CHILD_ENV, timeout: 120000, encoding: 'utf8' },
      (err, stdout) => {
        if (err) return resolve({ ok: false, error: String(err.message || err) })
        try { resolve({ ok: true, status: JSON.parse(stdout) }) } catch (e) { resolve({ ok: false, error: String(e) }) }
      })
  })
}

async function hostOrchestrator(runId) {
  const key = String(runId || 'default')
  if (orchestrators.has(key)) return await orchestrators.get(key)
  const services = hostServices || pluginCtx
  const agents = services && typeof services.get === 'function' ? services.get('agents') : null
  if (!agents || typeof agents.create !== 'function') throw new Error('agents service unavailable')
  let options = { meta: { cwd: BASE } }
  try {
    const defaults = pluginCtx && typeof pluginCtx.get === 'function' ? pluginCtx.get('agentDefaultModel') : null
    const selection = defaults && typeof defaults.currentSelection === 'function' ? defaults.currentSelection() : null
    let provider = selection && selection.provider ? selection.provider : ''
    let model = selection && selection.model ? selection.model : ''
    try {
      const profile = pluginCtx && typeof pluginCtx.get === 'function' ? pluginCtx.get('profileContext') : null
      if (profile && profile.name === 'desktop') provider = 'deepseek-account'
    } catch { /* keep the configured provider */ }
    if (provider && model) {
      const agentOptions = { provider, model }
      if (selection && selection.reasoningEffort) agentOptions.reasoningEffort = selection.reasoningEffort
      options = { meta: { cwd: BASE }, agentOptions }
    }
  } catch { /* fall back to factory defaults */ }
  const parentFile = bindingFile(RUNS, key)
  const saved = readBinding(parentFile)
  const sessionId = saved.parentSessionId || ('session-' + randomUUID())
  const pending = saved.parentSessionId
    ? (agents.get(sessionId) ? Promise.resolve({ agent: agents.get(sessionId) }) : agents.resume({ ...options, resumeSessionId: sessionId }))
    : agents.create(Object.assign({ sessionId }, options))
  orchestrators.set(key, pending)
  try {
    const created = await pending
    writeBinding(parentFile, { runId: key, parentSessionId: created.agent.session.id })
    hostLog({ stage: 'orchestrator-ready', run: key, sessionId: created && created.agent && created.agent.session && created.agent.session.id })
    return created
  } catch (err) {
    orchestrators.delete(key)
    throw err
  }
}

function hostLog(entry) {
  try {
    mkdirSync(RUNS, { recursive: true })
    writeFileSync(join(RUNS, 'host-agent.log'),
      JSON.stringify(Object.assign({ at: new Date().toISOString() }, entry)).slice(0, 20000) + '\n',
      { flag: 'a' })
  } catch { /* diagnostics only */ }
}

async function hostRunAgentOnce(body, signal) {
  const prompt = String((body && body.prompt) || '')
  const cwd = String((body && body.cwd) || '')
  if (!prompt) throw new Error('prompt required')
  if (!cwd) throw new Error('cwd required')
  if (!hostServices && !pluginCtx) throw new Error('host services not ready')
  const services = hostServices || pluginCtx
  const subagents = services.get('subagents')
  if (!subagents || typeof subagents.startActivation !== 'function') throw new Error('subagents service unavailable')
  const runId = String((body && body.run_id) || '')
  const handle = await hostOrchestrator(runId)
  const path = bindingFile(RUNS, runId, String(body.node_id || ''))
  const saved = readBinding(path)
  const query = services.get('sessionQuery')
  let unavailableReason = ''
  if (saved.childId && query) {
    const end = publicEvents(await observeEvents(query, saved.childId, signal)).filter(e => e.type === 'turn/end').at(-1)
    if (end?.reason === 'error' && /prompt variable.*model.*no value/i.test(end.error || '')) unavailableReason = '宿主旧会话缺少模型配置，已建立后继会话'
  }
  if (saved.childId && saved.cwd === cwd && !unavailableReason && query && typeof subagents.prompt === 'function') {
    return await continueChild({ subagents, query, parent: handle.agent, childId: saved.childId,
      prompt, requestId: body.request_id || randomUUID(), signal })
  }
  const startChild = (withPreset) => {
    const request = { parent: handle.agent, cwd, prompt: [{ type: 'text', text: prompt }] }
    const defaults = services.get('agentDefaultModel')
    const selection = defaults?.currentSelection?.()
    if (selection?.model) request.agentOptions = { provider: services.get('profileContext')?.name === 'desktop' ? 'deepseek-account' : selection.provider, model: selection.model, ...(selection.reasoningEffort ? { reasoningEffort: selection.reasoningEffort } : {}) }
    if (withPreset) request.agentPreset = 'cordis'
    return subagents.startActivation({
      provider: 'spawn',
      label: '画布节点 · ' + String((body && body.node_id) || ''),
      signal,
      request,
      delivery: 'caller',
    })
  }
  let activation
  try {
    activation = await startChild(true)
  } catch (err) {
    if (!/preset/i.test(String((err && err.message) || err))) throw err
    hostLog({ stage: 'preset-fallback', error: String((err && err.message) || err) })
    activation = await startChild(false)
  }
  writeBinding(path, { ...saved, runId, nodeId: body.node_id, cwd, continuationOf: saved.childId || null, childId: activation.childId, parentSessionId: handle.agent.session.id, continuityMode: saved.childId ? (unavailableReason ? 'successor_host_configuration' : 'successor_workspace_changed') : 'native_resume', continuityReason: unavailableReason || (saved.childId ? '成果代次的工作区已变化' : ''), status: 'running' })
  hostLog({ stage: 'activation', childId: activation.childId, cwd })
  const interruptChild = () => {
    try {
      subagents.interrupt(activation.childId, { kind: 'ancestor', agent: handle.agent })
      hostLog({ stage: 'interrupt', childId: activation.childId })
    } catch (err) {
      hostLog({ stage: 'interrupt-failed', childId: activation.childId, error: String((err && err.message) || err) })
    }
  }
  if (signal) {
    if (signal.aborted) interruptChild()
    else signal.addEventListener('abort', interruptChild, { once: true })
  }
  let result
  try {
    result = await activation.result
  } finally {
    if (signal) signal.removeEventListener('abort', interruptChild)
  }
  if (signal && signal.aborted) throw new Error('host subagent aborted')
  let diagnostic = ''
  if (!result || result.stopReason !== 'completed') {
    try {
      const children = await subagents.listChildren(handle.agent.session.id)
      diagnostic = String(JSON.stringify(children)).slice(0, 4000)
    } catch (err) {
      diagnostic = 'listChildren failed: ' + String((err && err.message) || err)
    }
    hostLog({ stage: 'child-failed', childId: activation.childId, stopReason: result && result.stopReason, diagnostic })
  }
  hostLog({ ok: true, childId: activation.childId, stopReason: result?.stopReason,
    publicOutputCount: Array.isArray(result?.output) ? result.output.filter(p => p.type === 'text').length : 0 })
  return { childId: activation.childId, parentSessionId: handle.agent.session.id, result: publicResult(result), diagnostic,
    continuityMode: saved.childId ? (unavailableReason ? 'successor_host_configuration' : 'successor_workspace_changed') : 'native_resume', continuationOf: saved.childId || null }
}

async function hostRunAgent(body, signal) {
  if (!body.run_id || !body.node_id) throw new Error('run_id and node_id required')
  const path = bindingFile(RUNS, body.run_id, body.node_id)
  const release = claimBinding(path)
  try {
    const saved = readBinding(path)
    if (body.request_id && saved.requestId === body.request_id && saved.response) return { ...saved.response, result: publicResult(saved.response.result) }
    if (['running', 'admitting', 'uncertain'].includes(saved.status)) throw new Error('上次执行结果未确认，请先同步会话并对账，避免重复执行')
    writeBinding(path, { ...saved, requestId: body.request_id || '', status: 'admitting', error: null })
    try {
      const response = await hostRunAgentOnce(body, signal)
      writeBinding(path, { ...readBinding(path), requestId: body.request_id || '', status: 'settled', response, error: null })
      return response
    } catch (error) {
      writeBinding(path, { ...readBinding(path), status: signal?.aborted ? 'cancelled' : 'uncertain', error: String(error.message || error) })
      throw error
    }
  } finally { release() }
}

async function handler(req, res) {
  if (!acceptOrigin(req, res)) return
  const path = (req.url || '/').split('?')[0]
  if (path.startsWith('/api/') && path !== '/api/health' && req.headers['x-canvas-token'] !== HOST_TOKEN)
    return sendJson(res, 403, { ok: false, error: '请通过Harness画布访问' })
  if (req.method === 'OPTIONS') {
    cors(res)
    res.writeHead(204)
    return res.end()
  }
  if (req.method === 'GET') {
    if (path === '/' || path === '/canvas' || path === '/workflow-canvas') {
      try {
        const html = readFileSync(CANVAS, 'utf8')
        cors(res)
        res.writeHead(200, {
          'content-type': 'text/html; charset=utf-8',
          'cache-control': 'no-store',
          'content-length': Buffer.byteLength(html),
        })
        return res.end(html)
      } catch (err) {
        return sendJson(res, 503, { ok: false, error: 'canvas missing: ' + CANVAS + ' — ' + String(err.message || err) })
      }
    }
    if (path === '/healthz') {
      res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8' })
      return res.end('ok')
    }
    if (path === '/api/health') {
      return sendJson(res, 200, { ok: true, service: 'canvas-host', port: server ? server.address().port : null, version: API_VERSION })
    }
    if (path === '/internal/session') {
      if (req.headers['x-canvas-token'] !== HOST_TOKEN) return sendJson(res, 403, { ok: false, error: 'forbidden' })
      const q = new URL(req.url, 'http://127.0.0.1').searchParams
      const binding = readBinding(bindingFile(RUNS, q.get('run_id'), q.get('node_id')))
      if (!binding.childId) return sendJson(res, 404, { ok: false, error: '模块尚无宿主会话绑定' })
      const services = hostServices || pluginCtx
      const query = services.get('sessionQuery')
      const events = await observeEvents(query, binding.childId)
      return sendJson(res, 200, { ok: true, binding: { ...binding, response: undefined },
        executing: isExecuting(events), events: publicEvents(events, Number(q.get('after') || -1)), cursor: events.at(-1)?.seq ?? -1 })
    }
    if (path === '/internal/selftest') {
      if (req.headers['x-canvas-token'] !== HOST_TOKEN) return sendJson(res, 403, { ok: false, error: 'forbidden' })
      const services = hostServices || pluginCtx
      const agents = services && typeof services.get === 'function' ? services.get('agents') : null
      const subagents = services && typeof services.get === 'function' ? services.get('subagents') : null
      const wantsCreate = new URL(req.url || '/', 'http://127.0.0.1').searchParams.get('create') === '1'
      const wantsChild = new URL(req.url || '/', 'http://127.0.0.1').searchParams.get('child') === '1'
      const wantsSpawn = new URL(req.url || '/', 'http://127.0.0.1').searchParams.get('spawn') === '1'
      const wantsPreset = new URL(req.url || '/', 'http://127.0.0.1').searchParams.get('preset') === '1'
      const providerParam = new URL(req.url || '/', 'http://127.0.0.1').searchParams.get('provider') || ''
      let defaultSelection = null
      try {
        const defaults = pluginCtx && typeof pluginCtx.get === 'function' ? pluginCtx.get('agentDefaultModel') : null
        defaultSelection = defaults && typeof defaults.currentSelection === 'function' ? defaults.currentSelection() : null
      } catch (err) {
        defaultSelection = 'ERR ' + String((err && err.message) || err)
      }
      let created = null
      let error = null
      let child = null
      let childError = null
      let spawnResult = null
      let spawnError = null
      if (wantsCreate) {
        try {
          const handle = await hostOrchestrator('selftest')
          created = handle && handle.agent && handle.agent.session ? handle.agent.session.id : null
        } catch (err) {
          error = String((err && err.message) || err)
        }
      }
      if (wantsChild) {
        try {
          const handle = await hostOrchestrator('selftest')
          const childHandle = await agents.create({
            sessionId: 'session-' + randomUUID(),
            parentAgent: handle.agent,
            meta: { cwd: BASE },
          })
          child = childHandle && childHandle.agent && childHandle.agent.session ? childHandle.agent.session.id : null
          await childHandle.dispose()
        } catch (err) {
          childError = String((err && err.stack) || err)
        }
      }
      if (wantsSpawn) {
        try {
          const handle = await hostOrchestrator('selftest')
          const dir = join(BASE, '.runs', 'selftest-ws')
          mkdirSync(dir, { recursive: true })
          const spawnRequest = { parent: handle.agent, cwd: dir, prompt: [{ type: 'text', text: 'Reply with exactly: OK' }] }
          if (wantsPreset) spawnRequest.agentPreset = 'standard'
          if (providerParam === 'account') spawnRequest.agentOptions = { provider: 'deepseek-account', model: 'deepseek-flash' }
          if (providerParam === 'official') spawnRequest.agentOptions = { provider: 'deepseek-official', model: 'deepseek-flash' }
          const spawnController = new AbortController()
          const activation = await subagents.startActivation({ provider: 'spawn', label: 'spawn-selftest', signal: spawnController.signal, request: spawnRequest, delivery: 'caller' })
          spawnResult = { childId: activation.childId, result: await activation.result }
        } catch (err) {
          spawnError = String((err && err.stack) || err)
        }
      }
      return sendJson(res, 200, {
        ok: true,
        capabilities: { resume: typeof agents?.resume === 'function', prompt: typeof subagents?.prompt === 'function', publicEvents: typeof services?.get('sessionQuery')?.observeSession === 'function' },
        agents: Boolean(agents && typeof agents.create === 'function'),
        subagents: Boolean(subagents && typeof subagents.startActivation === 'function'),
        orchestrator: orchestrators.size > 0,
        created,
        error,
        child,
        childError,
        spawnResult,
        spawnError,
        defaultSelection,
        version: API_VERSION,
      })
    }
    if (path === '/api/workflow') {
      return sendJson(res, 200, { ok: true, workflow: readJson(WORKFLOW, { stages: [] }), path: WORKFLOW })
    }
    if (path === '/api/layout') {
      return sendJson(res, 200, { ok: true, layout: readJson(LAYOUT, {}) })
    }
    if (path === '/api/status') {
      return sendJson(res, 200, await workflowStatus())
    }
    if (path === '/api/modules') {
      return sendJson(res, 200, readJson(MODULES, { modules: [] }))
    }
    if (path === '/api/canvas') {
      return sendJson(res, 200, { ok: true, graph: readJson(CANVAS_GRAPH, { nodes: [], edges: [] }) })
    }
    if (path === '/api/wf/health') {
      return sendJson(res, 200, { ok: true, ...(await wfHealth()) })
    }
    if (path === '/api/publish/check') {
      const u = new URL(req.url || '/', 'http://127.0.0.1')
      const episode = u.searchParams.get('episode') || 'day1'
      const data = await runJson(['wf.py', 'rehearse', episode], 120000)
      return sendJson(res, 200, { ok: Boolean(data.ok), data })
    }
    if (path === '/api/wf/status') {
      return sendJson(res, 200, { ok: true, run: latestRunState() })
    }
    if (path === '/api/gates') {
      const gates = await knownGates()
      return sendJson(res, 200, { ok: true, gates: Object.keys(gates).map((id) => ({ id, label: gates[id] })) })
    }
    if (path === '/api/library') {
      return sendJson(res, 200, { ok: true, library: readJson(LIBRARY, { blocks: [] }), path: LIBRARY })
    }
    return sendJson(res, 404, { ok: false, error: 'not found' })
  }
  if (req.method === 'POST') {
    let body
    try { body = await readBody(req) } catch (err) { return sendJson(res, 400, { ok: false, error: 'bad json: ' + String(err.message || err) }) }
    if (path === '/internal/agent') {
      if (req.headers['x-canvas-token'] !== HOST_TOKEN) return sendJson(res, 403, { ok: false, error: 'forbidden' })
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), 20 * 60 * 1000)
      const abortIfGone = () => { if (!res.writableEnded) controller.abort() }
      res.on('close', abortIfGone)
      try {
        return sendJson(res, 200, Object.assign({ ok: true }, await hostRunAgent(body, controller.signal)))
      } catch (err) {
        const message = String((err && err.message) || err)
        hostLog({ ok: false, error: message, stack: err && err.stack, aborted: controller.signal.aborted })
        return sendJson(res, 500, { ok: false, error: message })
      } finally {
        clearTimeout(timer)
        res.off('close', abortIfGone)
      }
    }
    if (path === '/api/workflow') {
      const wf = body.workflow || body
      const gates = await knownGates()
      const bad = validateWorkflow(wf)
      if (!bad && Object.keys(gates).length) {
        for (const st of wf.stages) {
          for (const req of st.requirements || []) {
            const g = (req || {}).gate || ''
            if (g && !(g in gates)) {
              return sendJson(res, 400, { ok: false, error: `未知闸门 ${g}；可用：${Object.keys(gates).join(', ')}` })
            }
          }
        }
      }
      const result = bad ? { code: 400, body: { ok: false, error: bad } } : saveWorkflow(wf)
      return sendJson(res, result.code, result.body)
    }
    if (path === '/api/wf/watch') {
      const started = spawnWf(['trigger', '--interval', String(body.interval || 180)])
      return sendJson(res, started ? 200 : 500, { ok: started, hint: '已启动自动监听（每 3 分钟扫描新 brief）' })
    }
    if (path === '/api/wf/run') {
      const episode = body.episode || 'day1'
      const args = ['run', '--episode', String(episode)]
      if (body.until) args.push('--until', String(body.until))
      const started = spawnWf(args)
      return sendJson(res, started ? 200 : 500, { ok: started, started: episode, until: body.until || null, hint: '稍后刷新状态' })
    }
    if (path === '/api/canvas') {
      const graph = body.graph || body
      if (!graph || !Array.isArray(graph.nodes) || graph.nodes.length === 0) {
        return sendJson(res, 400, { ok: false, error: 'canvas 需要非空 nodes' })
      }
      const old = readJson(CANVAS_GRAPH, {})
      writeFileSync(CANVAS_GRAPH, JSON.stringify(graph, null, 2), 'utf8')
      const r = await runPy(['wf.py', 'canvas', 'sync'])
      if (!r.ok) {
        writeFileSync(CANVAS_GRAPH, JSON.stringify(old, null, 2), 'utf8')
        return sendJson(res, 400, { ok: false, error: String(r.error || '').slice(0, 400) })
      }
      return sendJson(res, 200, { ok: true, graph: graph, synced: r.data })
    }
    if (path === '/api/canvas/add') {
      const args = ['wf.py', 'canvas', 'add', '--module', String(body.module || '')]
      if (body.after) args.push('--after', String(body.after))
      const r = await runPy(args)
      if (!r.ok) return sendJson(res, 400, { ok: false, error: String(r.error || '').slice(0, 300) })
      return sendJson(res, 200, { ok: true, graph: readJson(CANVAS_GRAPH, {}) })
    }
    if (path === '/api/canvas/remove') {
      const r = await runPy(['wf.py', 'canvas', 'remove', '--id', String(body.id || '')])
      return sendJson(res, r.ok ? 200 : 400, { ok: r.ok, graph: readJson(CANVAS_GRAPH, {}), error: r.ok ? null : String(r.error || '').slice(0, 300) })
    }
    if (path === '/api/canvas/connect') {
      const r = await runPy(['wf.py', 'canvas', 'connect', '--from', String(body.from || ''), '--to', String(body.to || '')])
      return sendJson(res, r.ok ? 200 : 400, { ok: r.ok, graph: readJson(CANVAS_GRAPH, {}), error: r.ok ? null : String(r.error || '').slice(0, 300) })
    }
    if (path === '/api/create') {
      const topic = String(body.topic || '').trim()
      if (!topic) return sendJson(res, 400, { ok: false, error: '缺 topic' })
      const args = ['wf.py', 'create', '--topic', topic,
                    '--duration', String(Number(body.duration) || 90),
                    '--type', String(body.type || 'tool_test')]
      if (body.episode) args.push('--episode', String(body.episode))
      const r = await runPyRaw(args)
      let data = null
      try { data = JSON.parse((r.stdout || '').trim()) } catch {
        try { data = JSON.parse((r.stdout || '').trim().split('\n').pop()) } catch {}
      }
      if (!data || !data.ok) {
        return sendJson(res, 400, { ok: false, error: (r.stderr || r.stdout || 'create failed').slice(0, 400) })
      }
      return sendJson(res, 200, data)
    }
    if (path === '/api/wf/approve') {
      const rid = body.run_id
      if (!rid) return sendJson(res, 400, { ok: false, error: '缺 run_id' })
      const r = await runWf(['approve', rid])
      if (!r.ok) return sendJson(res, 400, { ok: false, error: r.output.slice(0, 300) })
      spawnWf(['resume', rid])
      return sendJson(res, 200, { ok: true, approved: rid, hint: '已批准并继续运行' })
    }
    if (path === '/api/wf/reject') {
      const rid = body.run_id
      if (!rid) return sendJson(res, 400, { ok: false, error: '缺 run_id' })
      const r = await runWf(['reject', rid])
      return sendJson(res, r.ok ? 200 : 400, { ok: r.ok, output: r.output.slice(0, 300) })
    }
    if (path === '/api/library') {
      const library = body.library || body
      const bad = validateLibrary(library)
      if (bad) return sendJson(res, 400, { ok: false, error: bad })
      const ts = new Date().toISOString().replace(/[:T]/g, '-').slice(0, 19)
      const backup = join(BASE, 'kb', `spec-library.backup-${ts}.json`)
      try {
        mkdirSync(join(BASE, 'kb'), { recursive: true })
        if (existsSync(LIBRARY)) copyFileSync(LIBRARY, backup)
      } catch {}
      writeFileSync(LIBRARY, JSON.stringify(library, null, 2), 'utf8')
      const audit = await runPy(['spec_compose.py', '--audit'])
      if (!audit.ok) {
        try { if (existsSync(backup)) copyFileSync(backup, LIBRARY) } catch {}
        return sendJson(res, 400, { ok: false, error: 'spec_compose --audit 未通过，已回滚', detail: audit.error })
      }
      return sendJson(res, 200, { ok: true, backup, audit: audit.data })
    }
    if (path === '/api/layout') {
      const layout = body.layout || body
      if (!layout || typeof layout !== 'object' || Array.isArray(layout)) {
        return sendJson(res, 400, { ok: false, error: 'layout 必须是对象' })
      }
      try {
        writeFileSync(LAYOUT, JSON.stringify(layout, null, 2), 'utf8')
        return sendJson(res, 200, { ok: true })
      } catch (err) {
        return sendJson(res, 500, { ok: false, error: String(err.message || err) })
      }
    }
    return sendJson(res, 404, { ok: false, error: 'not found' })
  }
  return sendJson(res, 405, { ok: false, error: 'method not allowed' })
}

function listenOn(ports, index, done) {
  if (index >= ports.length) return done(new Error('no free loopback port'))
  const s = createServer((req, res) => {
    Promise.resolve()
      .then(() => handler(req, res))
      .catch((err) => {
        console.error('[workflow-canvas-sidebar] request failed:', err)
        try { sendJson(res, 500, { ok: false, error: String((err && err.message) || err) }) } catch { /* response already gone */ }
      })
  })
  s.once('error', () => {
    s.close()
    listenOn(ports, index + 1, done)
  })
  s.listen(ports[index], '127.0.0.1', () => done(null, s, ports[index]))
}

async function apiHealth() {
  try {
    return await readHttpJson('http://127.0.0.1:' + API_PORT + '/api/health', 1200, { 'x-canvas-api-token': API_SECRET })
  } catch {
    return null
  }
}

function matchesApi(health) {
  const canonical = (path) => typeof path === 'string' ? resolve(path).replaceAll('\\', '/').toLowerCase() : ''
  return Boolean(health && health.service === 'canvas-editor' && health.version === API_VERSION
    && health.build_id === BUILD_INFO.build_id && canonical(health.data_home) === canonical(BASE) && canonical(health.server_file) === canonical(SERVER))
}

// Cold starts can receive several requests at once (the canvas polls while the
// host is still booting). Python's HTTP server allows address reuse, so racing
// spawns would leave two API servers on the same port. Keep one start in flight.
let ensureInFlight = null

export async function ensurePythonApi() {
  if (ensureInFlight) return ensureInFlight
  ensureInFlight = startPythonApiOnce().finally(() => { ensureInFlight = null })
  return ensureInFlight
}

async function startPythonApiOnce() {
  if (PREVIOUS_INSTANCE.port) {
    API_PORT = Number(PREVIOUS_INSTANCE.port)
    if (matchesApi(await apiHealth())) return true
  }
  const preferred = [...new Set([Number(process.env.CANVAS_PORT || PLUGIN_CONFIG.port || 8793), 8797, 8798, 8799, 8800, 8801, 8802, 0])]
  for (const candidate of preferred) {
    API_PORT = candidate
    if (matchesApi(await apiHealth())) return true
    const available = await new Promise((resolvePort) => {
      const probe = createServer()
      probe.once('error', () => resolvePort(null))
      probe.listen(candidate, '127.0.0.1', () => { const port = probe.address().port; probe.close(() => resolvePort(port)) })
    })
    if (available) { API_PORT = available; break }
  }
  let health = await apiHealth()
  if (matchesApi(health)) return true
  if (health) {
    console.warn('[workflow-canvas-sidebar] 所选端口已被其他实例占用，未接管')
    return false
  }
  try {
    mkdirSync(RUNS, { recursive: true })
    const fd = openSync(join(RUNS, 'api-server.log'), 'a')
    const child = spawn(PY, [SERVER], {
      cwd: CODE_CWD, env: { ...CHILD_ENV, CANVAS_PORT: String(API_PORT), CANVAS_API_TOKEN: API_SECRET }, detached: true, windowsHide: true, stdio: ['ignore', fd, fd],
    })
    child.unref()
    closeSync(fd)
  } catch (err) {
    console.warn('[workflow-canvas-sidebar] cannot start api server:', err && err.message)
    return false
  }
  for (let i = 0; i < 25; i++) {
    await new Promise((resolve) => setTimeout(resolve, 200))
    health = await apiHealth()
    if (matchesApi(health)) {
      writeFileSync(INSTANCE_FILE, JSON.stringify({ port: API_PORT, token: API_SECRET, pid: health.pid,
        build_id: BUILD_INFO.build_id, data_home: BASE, server_file: SERVER }), { mode: 0o600 })
      return true
    }
  }
  console.warn('[workflow-canvas-sidebar] API did not become ready')
  return false
}

async function proxyApi(req, res) {
  if (!acceptOrigin(req, res)) return
  const port = server && server.address() ? server.address().port : null
  if (!port) {
    res.writeHead(503, { 'content-type': 'application/json' })
    return res.end(JSON.stringify({ ok: false, error: 'canvas server starting' }))
  }
  const chunks = []
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    for await (const c of req) chunks.push(c)
  }
  let apiReady = false
  try {
    const meta = await apiHealth()
    apiReady = matchesApi(meta)
  } catch {}
  if (!apiReady) {
    await ensurePythonApi()
    res.writeHead(503, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
    return res.end(JSON.stringify({ ok: false, error: '执行服务正在启动，请稍候再试' }))
  }
  const target = 'http://127.0.0.1:' + API_PORT + req.url.replace(/^\/dsh-workflow\/api/, '/api')
  try {
    await streamResponse(target, req, res, chunks.length ? Buffer.concat(chunks) : undefined, { 'x-canvas-api-token': API_SECRET })
  } catch (err) {
    if (!res.headersSent && !res.destroyed) {
      res.writeHead(502, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ ok: false, error: String(err && err.message || err) }))
    } else { res.destroy() }
  }
}

export function apply(ctx) {
  pluginCtx = ctx
  ensurePythonApi()
  const ports = process.env.WC_PORT ? [Number(process.env.WC_PORT)] : PREFERRED
  ctx.effect(() => {
    listenOn(ports, 0, (err, s, port) => {
      if (err) {
        console.warn('[workflow-canvas-sidebar] cannot start canvas server:', err.message)
        return
      }
      server = s
      url = 'http://127.0.0.1:' + port + '/'
      try {
        mkdirSync(RUNS, { recursive: true })
        writeFileSync(join(RUNS, 'host-agent.json'), JSON.stringify({
          url: 'http://127.0.0.1:' + port + '/internal/agent',
          token: HOST_TOKEN,
          version: API_VERSION,
        }))
      } catch (err) {
        console.warn('[workflow-canvas-sidebar] cannot write host-agent.json:', err && err.message)
      }
      console.log('[workflow-canvas-sidebar] canvas + editor API at ' + url)
    })
    return () => {
      try { if (server) server.close() } catch {}
      try {
        const marker = join(RUNS, 'host-agent.json')
        const published = JSON.parse(readFileSync(marker, 'utf8'))
        // A disposed instance must not erase the replacement instance's bridge.
        if (published.token === HOST_TOKEN) writeFileSync(marker, JSON.stringify({ url: '', token: '', version: API_VERSION }))
      } catch {}
      server = null
      url = null
    }
  }, 'workflow-canvas: loopback canvas + API')

  ctx.on('webserver/index-inject', (table) => {
    if (url) table.push({ kind: 'global', name: GLOBAL, value: url })
  })

  try {
    ctx.inject(['webServer'], (webCtx) => {
      if (!webCtx.webServer || typeof webCtx.webServer.register !== 'function') {
        console.warn('[workflow-canvas-sidebar] webServer.register unavailable')
        return
      }
      webCtx.effect(() => webCtx.webServer.register({
        kind: 'prefix',
        path: '/dsh-workflow/api',
        handler: proxyApi,
      }), 'workflow-panel: /dsh-workflow/api proxy')
    })
  } catch (err) {
    console.warn('[workflow-canvas-sidebar] webServer inject failed', err)
  }

  // Host-side agent bridge: AI nodes run as DSH subagent sessions under one
  // orchestrator agent per run, so they use the host's model and login
  // instead of a separate API key.
  try {
    ctx.inject(['agents', 'subagents'], (hostCtx) => {
      hostServices = hostCtx
      hostCtx.effect(() => () => {
        if (hostServices !== hostCtx) return
        hostServices = null
        for (const pending of orchestrators.values()) {
          try { Promise.resolve(pending).then((handle) => handle && handle.dispose()).catch(() => {}) } catch { /* already gone */ }
        }
        orchestrators.clear()
      }, 'workflow-canvas: host subagent bridge')
    })
  } catch (err) {
    console.warn('[workflow-canvas-sidebar] host agent bridge unavailable:', err && err.message)
  }

  ctx.effect(() => () => {
    pluginCtx = null
    for (const pending of orchestrators.values()) {
      try { Promise.resolve(pending).then((handle) => handle && handle.dispose()).catch(() => {}) } catch { /* already gone */ }
    }
    orchestrators.clear()
  }, 'workflow-canvas: host bridge lifecycle')
}
