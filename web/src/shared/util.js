/* 通用工具：字节格式化、文件 md5、读图片尺寸。 */
import { api } from './client'

export function fmtBytes(n) {
  if (n == null) return '-'
  const v = Number(n)
  if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(1)} GB`
  if (v >= 1024 ** 2) return `${(v / 1024 ** 2).toFixed(1)} MB`
  if (v >= 1024) return `${(v / 1024).toFixed(0)} KB`
  return `${v} B`
}

/* ---------- 纯 JS 增量 MD5 ----------
   说明：Web Crypto API 不支持 MD5，且非安全上下文（HTTP 局域网访问）下
   crypto.subtle 为 undefined（生产相册上传即因此报错）。故用纯 JS 实现，
   增量处理分片，大文件不整读进内存。 */

const MD5_S = [
  7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
  5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
  4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
  6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
]
const MD5_T = []
for (let i = 0; i < 64; i++) MD5_T[i] = (Math.floor(Math.abs(Math.sin(i + 1)) * 0x100000000)) | 0
const MD5_X = []
for (let i = 0; i < 16; i++) {
  MD5_X[i] = i
  MD5_X[i + 16] = (5 * i + 1) % 16
  MD5_X[i + 32] = (3 * i + 5) % 16
  MD5_X[i + 48] = (7 * i) % 16
}

function md5Add(a, b) {
  return (a + b) >>> 0
}
function md5Rotl(n, s) {
  return ((n << s) | (n >>> (32 - s))) >>> 0
}
function md5Fn(f, x, y, z) {
  let r
  switch (f) {
    case 0: r = (x & y) | (~x & z); break
    case 1: r = (x & z) | (y & ~z); break
    case 2: r = x ^ y ^ z; break
    default: r = y ^ (x | ~z)
  }
  return r >>> 0
}
function md5ProcessBlock(state, block) {
  const X = new Array(16)
  for (let i = 0; i < 16; i++) {
    X[i] = (block[i * 4] | (block[i * 4 + 1] << 8) | (block[i * 4 + 2] << 16) | (block[i * 4 + 3] << 24)) | 0
  }
  let a = state[0]
  let b = state[1]
  let c = state[2]
  let d = state[3]
  for (let i = 0; i < 64; i++) {
    let t = md5Add(md5Add(a, md5Fn(i >> 4, b, c, d)), 0)
    t = md5Add(t, X[MD5_X[i]])
    t = md5Add(t, MD5_T[i])
    const nb = md5Add(b, md5Rotl(t, MD5_S[i]))
    a = d
    d = c
    c = b
    b = nb
  }
  state[0] = md5Add(state[0], a)
  state[1] = md5Add(state[1], b)
  state[2] = md5Add(state[2], c)
  state[3] = md5Add(state[3], d)
}

function createMd5() {
  const state = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476]
  let buffer = new Uint8Array(0)
  let totalBytes = 0
  return {
    update(data) {
      totalBytes += data.length
      const combined = new Uint8Array(buffer.length + data.length)
      combined.set(buffer, 0)
      combined.set(data, buffer.length)
      const nBlocks = Math.floor(combined.length / 64)
      for (let i = 0; i < nBlocks * 64; i += 64) {
        md5ProcessBlock(state, combined.subarray(i, i + 64))
      }
      buffer = combined.slice(nBlocks * 64)
      return this
    },
    digest() {
      const bitLenLo = (totalBytes * 8) >>> 0
      const bitLenHi = Math.floor((totalBytes * 8) / 0x100000000) >>> 0
      // 0x80 之后补零，使整体 ≡ 56 (mod 64)，再附 8 字节小端长度
      const padLen = (56 - ((buffer.length + 1) % 64) + 64) % 64
      const lenBytes = new Uint8Array(8)
      lenBytes[0] = bitLenLo & 0xff
      lenBytes[1] = (bitLenLo >>> 8) & 0xff
      lenBytes[2] = (bitLenLo >>> 16) & 0xff
      lenBytes[3] = (bitLenLo >>> 24) & 0xff
      lenBytes[4] = bitLenHi & 0xff
      lenBytes[5] = (bitLenHi >>> 8) & 0xff
      lenBytes[6] = (bitLenHi >>> 16) & 0xff
      lenBytes[7] = (bitLenHi >>> 24) & 0xff
      const final = new Uint8Array(buffer.length + 1 + padLen + 8)
      final.set(buffer, 0)
      final[buffer.length] = 0x80
      final.set(lenBytes, buffer.length + 1 + padLen)
      for (let i = 0; i < final.length; i += 64) {
        md5ProcessBlock(state, final.subarray(i, i + 64))
      }
      let hex = ''
      for (let i = 0; i < 4; i++) {
        const w = state[i]
        hex += (w & 0xff).toString(16).padStart(2, '0')
        hex += ((w >>> 8) & 0xff).toString(16).padStart(2, '0')
        hex += ((w >>> 16) & 0xff).toString(16).padStart(2, '0')
        hex += ((w >>> 24) & 0xff).toString(16).padStart(2, '0')
      }
      return hex
    },
  }
}

/* 计算文件 md5（分片增量，纯 JS，兼容 HTTP 局域网/非安全上下文） */
export function fileMd5(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    const CHUNK = 4 * 1024 * 1024
    const total = Math.ceil(file.size / CHUNK)
    let idx = 0
    const md5 = createMd5()
    reader.onerror = () => reject(new Error('read failed'))
    reader.onload = () => {
      md5.update(new Uint8Array(reader.result))
      idx++
      if (idx < total) readNext()
      else resolve(md5.digest())
    }
    function readNext() {
      const start = idx * CHUNK
      reader.readAsArrayBuffer(file.slice(start, Math.min(file.size, start + CHUNK)))
    }
    readNext()
  })
}

/* 用 Image 对象读取图片宽高（尺寸>0 视为可解析） */
export function imageSize(file) {
  return new Promise((resolve) => {
    if (!file.type.startsWith('image/')) return resolve({ width: 0, height: 0 })
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => resolve({ width: img.naturalWidth, height: img.naturalHeight })
    img.onerror = () => resolve({ width: 0, height: 0 })
    img.src = url
  })
}

/* 浏览器端 CRC32（单块校验，SPEC-UPLOAD-TRANSPORT.md §2 块级） */
const CRC_TABLE = (() => {
  const t = new Uint32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    t[n] = c >>> 0
  }
  return t
})()

export function crc32Hex(bytes) {
  let c = 0xffffffff
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8)
  // >>> 0 归一化为无符号后再转 hex，否则高位 CRC（≥0x80000000）会输出负值（如 "-4791d14"）
  return ((c ^ 0xffffffff) >>> 0).toString(16).padStart(8, '0')
}

async function retry(fn, attempts = 5) {
  let lastErr
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn()
    } catch (e) {
      lastErr = e
      if (i < attempts - 1) await new Promise((r) => setTimeout(r, Math.min(30000, 2000 * 2 ** i)))
    }
  }
  throw lastErr
}

/* 分片上传：photos 与 drive 通用（SPEC §4：并发 + CRC32 + 断点续传 + 秒传）。 */
export async function chunkedUpload({
  initUrl, chunkUrl, completeUrl, file, md5, onProgress, extraInit,
  concurrency = 4, uploadId,
}) {
  const init = await api.post(initUrl, { md5, uploadId, ...(extraInit || {}) })
  if (init.code !== 0) throw new Error(init.msg || 'init 失败')
  const id = init.data.uploadId
  const CHUNK = 4 * 1024 * 1024
  const total = Math.ceil(file.size / CHUNK)

  // 秒传：服务端已存在同内容 → 直接完成
  if (init.data.exists) {
    onProgress?.(100)
    return init.data
  }

  const have = new Set(init.data.haveBlocks || [])
  let done = have.size

  async function uploadChunk(seq) {
    const start = seq * CHUNK
    const part = file.slice(start, Math.min(file.size, start + CHUNK))
    const buf = new Uint8Array(await part.arrayBuffer())
    const crc = crc32Hex(buf)
    const blob = new Blob([buf])
    await retry(async () => {
      const res = await api.putBinary(chunkUrl(id, seq), blob, { 'X-Chunk-CRC32': crc })
      if (!res.ok) throw new Error(`分片 ${seq} 上传失败`)
    })
    done += 1
    onProgress?.(Math.round((done / total) * 100))
  }

  // 待传块队列，固定并发
  const pending = []
  for (let seq = 0; seq < total; seq++) if (!have.has(seq)) pending.push(seq)
  let cursor = 0
  async function worker() {
    while (cursor < pending.length) {
      const seq = pending[cursor]
      cursor += 1
      await uploadChunk(seq)
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, pending.length) }, worker))

  const doneResp = await api.post(completeUrl(id), {})
  if (doneResp.code !== 0) throw new Error(doneResp.msg || '合并失败')
  return doneResp.data
}
