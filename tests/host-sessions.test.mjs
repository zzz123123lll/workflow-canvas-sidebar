import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { bindingFile, writeBinding, readBinding, claimBinding, publicEvents, publicResult, continueChild } from '../host-sessions.js'

test('durable bindings are scoped and simultaneous execution is rejected', () => {
  const root = mkdtempSync(join(tmpdir(), 'canvas-bindings-'))
  try {
    const path = bindingFile(root, 'run', 'node')
    assert.notEqual(path, bindingFile(root, 'run-other', 'node'))
    writeBinding(path, { childId: 'real-child' })
    assert.equal(readBinding(path).childId, 'real-child')
    const release = claimBinding(path)
    assert.throws(() => claimBinding(path), /已有执行器/)
    release()
    claimBinding(path)()
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('only public text and tool names leave the event adapter', () => {
  const rows = publicEvents([{ seq: 1, type: 'assistant/message', data: { message: { content: [
    { type: 'reasoning', text: 'PRIVATE' }, { type: 'text', text: 'Public reply' }] } } },
  { seq: 2, type: 'tool/call', data: { name: 'read_file', arguments: 'PRIVATE' } }])
  assert.equal(rows[0].text, 'Public reply')
  assert.equal(rows[1].tool, 'read_file')
  assert.ok(!JSON.stringify(rows).includes('PRIVATE'))
  assert.ok(!JSON.stringify(publicResult({ output: [{type:'reasoning',text:'PRIVATE'}, {type:'text',text:'Public'}] })).includes('PRIVATE'))
})

test('continuation keeps child identity and waits for actual turn completion', async () => {
  let reads = 0, disposed = 0, request
  const query = { observeSession: async () => ({ events: ++reads === 1
    ? [{ seq: 0, type: 'turn/end' }]
    : [{ seq: -2, type: 'assistant/message', data: { message: { content: [{ type: 'text', text: 'Unrelated result' }] } } },
      { seq: 0, type: 'turn/end' }, { seq: 0.5, type: 'user/message', data: { id: 'message' } },
      { seq: 1, type: 'assistant/message', data: { message: { content: [{ type: 'text', text: 'New result' }] } } },
      { seq: 2, type: 'turn/end', data: { reason: { kind: 'completed' } } }], [Symbol.dispose]() { disposed++ } }) }
  const result = await continueChild({ query, subagents: { prompt: async r => { request = r; return { messageId: 'message' } } },
    parent: { session: { id: 'parent' } }, childId: 'child', prompt: 'Continue', requestId: 'command' })
  assert.equal(request.requestId, 'command')
  assert.equal(result.childId, 'child')
  assert.equal(result.result.output[0].text, 'New result')
  assert.equal(disposed, 2)
})

test('an active native conversation cannot receive a second executor', async () => {
  const query = { observeSession: async () => ({ events: [{ seq: 0, type: 'turn/start' }], [Symbol.dispose]() {} }) }
  await assert.rejects(continueChild({ query, subagents: {}, parent: {}, childId: 'child' }), /正在执行/)
})
