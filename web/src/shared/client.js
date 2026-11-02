/* 统一 fetch 封装（SPEC-WEB-FRONTEND.md §4）：同源、Bearer 注入、401 全局拦截。 */

const TOKEN_KEY = 'device_token'
const DEVICE_ID_KEY = 'device_id'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token, deviceId) {
  localStorage.setItem(TOKEN_KEY, token)
  if (deviceId) localStorage.setItem(DEVICE_ID_KEY, deviceId)
}

export function clearSession() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(DEVICE_ID_KEY)
}

let redirecting = false
export function redirectToPair() {
  clearSession()
  if (!redirecting) {
    redirecting = true
    window.location.href = '/portal/pair.html'
  }
}

async function request(path, { method = 'GET', body, headers = {}, binary = false } = {}) {
  const token = getToken()
  const init = { method, headers: { ...headers } }
  if (token) init.headers['Authorization'] = `Bearer ${token}`
  if (body != null) {
    if (binary) {
      init.body = body // Blob / FormData，不设 Content-Type
    } else {
      init.headers['Content-Type'] = 'application/json'
      init.body = JSON.stringify(body)
    }
  }
  const res = await fetch(path, init)
  if (res.status === 401) {
    redirectToPair()
    throw new Error('unauthorized')
  }
  if (binary) return res
  const json = await res.json().catch(() => ({ code: 1, msg: 'invalid response' }))
  if (json.code === 10401) {
    redirectToPair()
    throw new Error(json.msg || 'unauthorized')
  }
  return json
}

export const api = {
  get: (path, params) => {
    const qs = params ? '?' + new URLSearchParams(params).toString() : ''
    return request(path + qs)
  },
  post: (path, body) => request(path, { method: 'POST', body }),
  put: (path, body) => request(path, { method: 'PUT', body }),
  del: (path, params) => {
    const qs = params ? '?' + new URLSearchParams(params).toString() : ''
    return request(path + qs, { method: 'DELETE' })
  },
  // 二进制上传（分片/整包），返回原始 Response
  putBinary: (path, blob, headers = {}) => request(path, { method: 'PUT', body: blob, binary: true, headers }),
  fetchBinary: (path) => request(path, { binary: true }),
}

/* 配对：POST /api/devices/pair */
export async function pairDevice(code, name) {
  const body = { code }
  const deviceId = localStorage.getItem(DEVICE_ID_KEY)
  if (deviceId) body.deviceId = deviceId
  if (name) body.name = name
  const json = await request('/api/devices/pair', { method: 'POST', body })
  if (json.code === 0) {
    setToken(json.data.token, json.data.deviceId)
    return json.data
  }
  throw new Error(json.msg || '配对失败')
}
