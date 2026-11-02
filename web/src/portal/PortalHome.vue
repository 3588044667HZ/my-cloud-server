<template>
  <div class="portal">
    <header class="top">
      <span class="logo">☁️ 云服务</span>
      <el-button text @click="logout">退出</el-button>
    </header>
    <main class="body">
      <section class="welcome">
        <div class="avatar">{{ initial }}</div>
        <div>
          <h1>你好{{ name ? '，' + name : '' }}</h1>
          <p class="sub">我的云端空间 · 设备 {{ deviceName || '—' }}</p>
        </div>
      </section>

      <section class="apps">
        <h2>我的应用</h2>
        <div v-if="apps.length" class="grid">
          <div v-for="a in apps" :key="a.id" class="app-item" @click="go(a)">
            <div class="app-grid-icon" :style="{ background: gradient(a.id) }">
              <img v-if="a.icon" :src="a.icon" alt="" />
              <span v-else>{{ emoji(a.id) }}</span>
            </div>
            <div class="app-name">{{ a.name }}</div>
            <div class="app-desc">{{ a.desc }}</div>
          </div>
        </div>
        <el-skeleton v-else :rows="2" animated />
      </section>
    </main>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api, clearSession, getToken } from '../shared/client'

const apps = ref([])
const deviceName = ref('')

const name = ''
const initial = '我'

const EMOJI = { album: '🖼️', drive: '📁', note: '📝', admin: '⚙️' }
const GRAD = {
  album: 'linear-gradient(135deg,#36d1dc,#5b86e5)',
  drive: 'linear-gradient(135deg,#f7971e,#ffd200)',
  note: 'linear-gradient(135deg,#11998e,#38ef7d)',
  admin: 'linear-gradient(135deg,#7f00ff,#e100ff)',
}
const emoji = (id) => EMOJI[id] || '📦'
const gradient = (id) => GRAD[id] || 'linear-gradient(135deg,#8e9eab,#eef2f3)'

function go(app) {
  window.location.href = app.url
}

function logout() {
  clearSession()
  window.location.href = '/portal/pair.html'
}

onMounted(async () => {
  if (!getToken()) {
    window.location.href = '/portal/pair.html'
    return
  }
  try {
    const r = await api.get('/api/portal/apps')
    if (r.code === 0) apps.value = r.data
    const me = await api.get('/api/devices/me')
    if (me.code === 0) deviceName.value = me.data.name
  } catch (e) {
    /* 401 已由 client 处理 */
  }
})
</script>

<style scoped>
.portal { min-height: 100vh; }
.top {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  background: #fff;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
}
.logo { font-weight: 700; font-size: 18px; }
.body { max-width: 1080px; margin: 0 auto; padding: 28px 20px; }
.welcome { display: flex; align-items: center; gap: 20px; margin-bottom: 32px; }
.avatar {
  width: 80px; height: 80px; border-radius: 50%;
  background: linear-gradient(135deg, #36d1dc, #5b86e5);
  color: #fff; font-size: 32px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.welcome h1 { margin: 0 0 4px; font-size: 24px; }
.sub { margin: 0; color: #909399; }
.apps h2 { font-size: 18px; margin-bottom: 16px; }
.grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 20px; }
@media (max-width: 900px) { .grid { grid-template-columns: repeat(3, 1fr); } }
.app-item {
  background: #fff; border-radius: 16px; padding: 24px 8px;
  text-align: center; cursor: pointer; transition: transform .15s, box-shadow .15s;
}
.app-item:hover { transform: translateY(-4px); box-shadow: 0 10px 24px rgba(0,0,0,.1); }
.app-item img { width: 64px; height: 64px; border-radius: 18px; }
.app-name { font-weight: 600; }
.app-desc { font-size: 12px; color: #909399; margin-top: 4px; }
</style>
