import { request } from 'node:http'

export function readJson(url, timeout = 1200, headers = {}) {
  return new Promise((resolve, reject) => {
    const req = request(url, { agent: false, headers }, (res) => {
      const chunks = []; let length = 0
      res.on('data', (chunk) => {
        length += chunk.length
        if (length > 1024 * 1024) res.destroy(new Error('Local response too large'))
        else chunks.push(chunk)
      })
      res.on('error', reject)
      res.on('end', () => {
        try { resolve(res.statusCode === 200 ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : null) }
        catch (error) { reject(error) }
      })
    })
    req.setTimeout(timeout, () => req.destroy(new Error('Local response timed out')))
    req.on('error', reject)
    req.end()
  })
}

export function streamResponse(url, req, res, body, authorizationHeaders = {}) {
  return new Promise((resolve, reject) => {
    const headers = { ...authorizationHeaders }
    for (const key of ['content-type', 'range', 'if-range']) if (req.headers[key]) headers[key] = req.headers[key]
    if (body?.length) headers['content-length'] = body.length
    const upstream = request(url, { method: req.method, headers, agent: false }, (incoming) => {
      const out = { 'cache-control': 'no-store' }
      for (const key of ['content-type', 'content-length', 'content-range', 'accept-ranges', 'content-disposition', 'last-modified']) {
        if (incoming.headers[key] != null) out[key] = incoming.headers[key]
      }
      res.writeHead(incoming.statusCode, out)
      incoming.on('error', (error) => { res.destroy(); reject(error) })
      incoming.on('end', resolve)
      res.once('close', () => { incoming.destroy(); upstream.destroy(); resolve() })
      incoming.pipe(res)
    })
    upstream.setTimeout(120000, () => upstream.destroy(new Error('Local response timed out')))
    upstream.on('error', reject)
    req.once('aborted', () => { upstream.destroy(); resolve() })
    upstream.end(body)
  })
}
