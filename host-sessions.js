// Public host services only. No direct access to Harness session files.
import { readFileSync, writeFileSync, mkdirSync, renameSync, openSync, closeSync, unlinkSync } from 'node:fs'
import { join } from 'node:path'
import { createHash, randomUUID } from 'node:crypto'

export function bindingFile(root, runId, nodeId = '') {
  const key = createHash('sha256').update(JSON.stringify([runId, nodeId])).digest('hex')
  return join(root, '_host-bindings', key + '.json')
}
export function readBinding(path) {
  try { return JSON.parse(readFileSync(path, 'utf8')) } catch (error) {
    if (error.code === 'ENOENT') return {}
    throw error
  }
}
export function writeBinding(path, value) {
  mkdirSync(join(path, '..'), { recursive: true })
  const temporary = path + '.tmp-' + randomUUID()
  writeFileSync(temporary, JSON.stringify(value, null, 2), 'utf8')
  renameSync(temporary, path)
}
export function claimBinding(path) {
  mkdirSync(join(path, '..'), { recursive: true })
  const lock = path + '.lock'
  try {
    const fd = openSync(lock, 'wx'); writeFileSync(fd, String(process.pid)); closeSync(fd)
  } catch (error) {
    if (error.code !== 'EEXIST') throw error
    const pid = Number(readFileSync(lock, 'utf8'))
    if (!Number.isInteger(pid) || pid <= 0) throw new Error('模块执行租约损坏，需要对账')
    try { process.kill(pid, 0); throw new Error('模块已有执行器，请等待或停止后继续') } catch (alive) {
      if (alive.code !== 'ESRCH') throw alive
    }
    unlinkSync(lock)
    return claimBinding(path)
  }
  return () => { try { unlinkSync(lock) } catch (error) { if (error.code !== 'ENOENT') throw error } }
}
export async function observeEvents(query, sessionId, signal) {
  const lease = await query.observeSession(sessionId, { projectionMode: 'none', signal })
  try { return Array.from(lease.events) } finally { lease[Symbol.dispose]() }
}
export function publicEvents(events, after = -1) {
  return events.filter(e => e.seq > after).flatMap(e => {
    const parts = e.type === 'assistant/message' ? e.data?.message?.content : e.data?.content
    if (e.type === 'assistant/message' || e.type === 'user/message') {
      const text = (Array.isArray(parts) ? parts : []).filter(p => p.type === 'text').map(p => p.text).join('\n')
      return text ? [{ seq: e.seq, at: e.time, type: e.type, text }] : []
    }
    if (e.type === 'tool/call') return [{ seq: e.seq, at: e.time, type: e.type, tool: e.data?.name || '' }]
    if (e.type === 'tool/result') {
      const text = (e.data?.message?.content || []).filter(p => p.type === 'text').map(p => p.text).join('\n')
      return [{ seq: e.seq, at: e.time, type: e.type, text: text.slice(0, 1600),
        truncated: text.length > 1600, error: e.data?.error?.name || '' }]
    }
    if (e.type === 'turn/end') return [{ seq: e.seq, at: e.time, type: e.type, reason: e.data?.reason?.kind || '',
      error: e.data?.reason?.kind === 'error' ? String(e.data.reason.error?.message || '').slice(0, 1600) : '' }]
    return []
  })
}
export function isExecuting(events) {
  const last = events.filter(e => e.type === 'turn/start' || e.type === 'turn/end').at(-1)
  return last?.type === 'turn/start'
}
export function publicResult(result) {
  return { stopReason: result?.stopReason,
    output: Array.isArray(result?.output) ? result.output.filter(p => p.type === 'text').map(p => ({ type: 'text', text: p.text })) : [] }
}
export async function continueChild({ subagents, query, parent, childId, prompt, requestId, signal }) {
  const interrupt = () => subagents.interrupt(childId, { kind: 'ancestor', agent: parent })
  signal?.addEventListener('abort', interrupt, { once: true })
  try {
  signal?.throwIfAborted()
  const before = await observeEvents(query, childId, signal)
  if (isExecuting(before)) throw new Error('原生会话正在执行，请等待完成后同步')
  const cursor = before.at(-1)?.seq ?? -1
  const accepted = await subagents.prompt({ parentSessionId: parent.session.id, childSessionId: childId,
    mode: 'continuable', requestId, delivery: 'queue', content: [{ type: 'text', text: prompt }] }, signal)
  for (;;) {
    if (signal?.aborted) {
      subagents.interrupt(childId, { kind: 'ancestor', agent: parent })
      throw new Error('host subagent aborted')
    }
    const events = await observeEvents(query, childId, signal)
    const tail = events.filter(e => e.seq > cursor)
    // A native UI turn can race with admission. Only settle the turn after
    // this command's accepted user message, never an unrelated turn/end.
    const admitted = tail.find(e => e.type === 'user/message' &&
      (e.data?.id === accepted.messageId || e.data?.source?.rpcId === requestId))
    const ownTail = admitted ? tail.filter(e => e.seq > admitted.seq) : []
    const end = ownTail.find(e => e.type === 'turn/end')
    const admissionError = !admitted && tail.find(e => e.type === 'turn/end' && e.data?.reason?.kind === 'error')
    if (admissionError) throw new Error('宿主未接收本次消息：' + String(admissionError.data.reason.error?.message || '会话启动错误'))
    if (end) {
      const output = publicEvents(ownTail.filter(e => e.seq <= end.seq)).filter(e => e.type === 'assistant/message').map(e => ({ type: 'text', text: e.text }))
      return { childId, parentSessionId: parent.session.id, messageId: accepted.messageId,
        result: { output, stopReason: end.data?.reason?.kind }, continuityMode: 'native_resume' }
    }
    await new Promise(resolve => setTimeout(resolve, 300))
  }
  } finally { signal?.removeEventListener('abort', interrupt) }
}
