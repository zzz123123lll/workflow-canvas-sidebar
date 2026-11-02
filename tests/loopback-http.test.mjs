import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer, request } from 'node:http'
import { once } from 'node:events'
import { readJson, streamResponse } from '../loopback-http.js'

test('loopback streams preserve ranges, head, POST and repeated client cancellation', async () => {
  let upstreamClosed = 0
  const origin = createServer(async (req, res) => {
    if (req.url === '/health') return res.end(JSON.stringify({ service: 'canvas-editor' }))
    if (req.url === '/post') { const parts=[]; for await (const p of req) parts.push(p); return res.end(Buffer.concat(parts)) }
    if (req.url === '/slow') {
      const timer=setInterval(() => res.write(Buffer.alloc(4096)), 2)
      res.on('close', () => { clearInterval(timer); upstreamClosed++ }); return
    }
    res.writeHead(206, { 'content-range':'bytes 2-5/8', 'content-length':4, 'accept-ranges':'bytes', 'content-type':'video/mp4' })
    res.end(req.method === 'HEAD' ? undefined : Buffer.from('cdef'))
  }).listen(0, '127.0.0.1')
  await once(origin, 'listening')
  const base=`http://127.0.0.1:${origin.address().port}`
  const proxy=createServer(async (req,res) => {
    const parts=[]; for await (const p of req) parts.push(p)
    try { await streamResponse(base+req.url, req, res, Buffer.concat(parts)) }
    catch { if (!res.headersSent) res.writeHead(502); res.end() }
  }).listen(0, '127.0.0.1')
  await once(proxy, 'listening')
  const target=`http://127.0.0.1:${proxy.address().port}`
  const get=(path, options={}, body) => new Promise((resolve,reject) => {
    const req=request(target+path, options, res => { const chunks=[];res.on('data',p=>chunks.push(p));res.on('end',()=>resolve({status:res.statusCode,headers:res.headers,body:Buffer.concat(chunks).toString()})) })
    req.on('error',reject); req.end(body)
  })
  try {
    assert.equal((await readJson(base+'/health')).service,'canvas-editor')
    const range=await get('/video',{headers:{range:'bytes=2-5'}})
    assert.equal(range.status,206);assert.equal(range.headers['content-range'],'bytes 2-5/8');assert.equal(range.body,'cdef')
    assert.equal((await get('/video',{method:'HEAD'})).body,'')
    assert.equal((await get('/post',{method:'POST',headers:{'content-type':'application/json'}},'{"hello":"中文"}')).body,'{"hello":"中文"}')
    for(let i=0;i<20;i++) await new Promise((resolve,reject)=>{
      const req=request(target+'/slow', res=>res.once('data',()=>{res.destroy();resolve()}));req.on('error',reject);req.end()
    })
    await new Promise(resolve=>setTimeout(resolve,30))
    assert.equal(upstreamClosed,20)
    assert.equal((await get('/video')).body,'cdef')
  } finally { proxy.closeAllConnections(); origin.closeAllConnections(); await Promise.all([new Promise(r=>proxy.close(r)),new Promise(r=>origin.close(r))]) }
})
