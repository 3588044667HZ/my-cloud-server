import axios from 'axios'

const api = axios.create({
  baseURL: '/api/admin',
  timeout: 15000,
})

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('admin_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem('admin_token')
      routerToLogin()
    }
    return Promise.reject(err)
  }
)

function routerToLogin() {
  if (window.location.hash !== '#/login') {
    window.location.hash = '#/login'
  }
}

// ---- 登录 ----
export function loginAdmin(password) {
  return api.post('/login', { password }).then((r) => r.data.data)
}

// ---- 统计 ----
export function getStats() {
  return api.get('/stats').then((r) => r.data.data)
}

// ---- 设备管理 ----
export function getDevices() {
  return api.get('/devices').then((r) => r.data.data)
}

export function genPairCode() {
  return api.post('/devices/pair-code').then((r) => r.data.data)
}

export function renameDevice(deviceId, name) {
  return api.post(`/devices/${deviceId}/rename`, { name }).then((r) => r.data.data)
}

export function revokeDevice(deviceId) {
  return api.delete(`/devices/${deviceId}`).then((r) => r.data.data)
}

export function getOrphans() {
  return api.get('/devices/orphans').then((r) => r.data.data)
}

export function cleanupOrphans() {
  return api.post('/devices/orphans/cleanup').then((r) => r.data.data)
}

// ---- 笔记管理 ----
export function getAdminNotes(params) {
  return api.get('/notes', { params }).then((r) => r.data)
}

export function deleteAdminNote(noteId) {
  return api.delete(`/notes/${noteId}`).then((r) => r.data.data)
}

// ---- 云盘 / 硬盘管理 ----
export function getDriveEnabled() {
  return api.get('/drive/enabled').then((r) => r.data.data)
}

export function setDriveEnabled(enabled) {
  return api.put('/drive/enabled', { enabled }).then((r) => r.data.data)
}

export function getShares() {
  return api.get('/drive/shares').then((r) => r.data.data)
}

export function addShare(payload) {
  return api.post('/drive/shares', payload).then((r) => r.data.data)
}

export function updateShare(name, payload) {
  return api.put(`/drive/shares/${name}`, payload).then((r) => r.data.data)
}

export function deleteShare(name) {
  return api.delete(`/drive/shares/${name}`).then((r) => r.data.data)
}

export default api
