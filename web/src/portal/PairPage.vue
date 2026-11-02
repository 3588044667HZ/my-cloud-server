<template>
  <div class="pair-wrap">
    <el-card class="pair-card" shadow="always">
      <h2 class="title">设备配对</h2>
      <p class="sub">配对码请在服务器管理后台 → 设备管理 → 生成配对码 获取</p>
      <el-form @submit.prevent="doPair">
        <el-form-item>
          <el-input
            v-model="code"
            size="large"
            placeholder="例如 AB3K-D9QX"
            @keyup.enter="doPair"
          />
        </el-form-item>
        <el-button
          type="primary"
          size="large"
          class="pair-btn"
          :loading="loading"
          @click="doPair"
        >
          连接
        </el-button>
      </el-form>
      <p v-if="error" class="error">{{ error }}</p>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { getToken, pairDevice } from '../shared/client'

const code = ref('')
const loading = ref(false)
const error = ref('')

onMounted(() => {
  // 已配对：直接回聚合页
  if (getToken()) window.location.href = '/'
})

async function doPair() {
  const clean = code.value.trim()
  if (!clean) return
  loading.value = true
  error.value = ''
  try {
    await pairDevice(clean, '浏览器')
    window.location.href = '/'
  } catch (e) {
    error.value = e.message || '配对失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.pair-wrap { height: 100vh; display: flex; align-items: center; justify-content: center; }
.pair-card { width: 400px; padding: 12px 8px; }
.title { text-align: center; margin: 0 0 6px; }
.sub { text-align: center; color: #909399; font-size: 13px; margin: 0 0 20px; }
.pair-btn { width: 100%; }
.error { color: #f56c6c; text-align: center; margin-top: 12px; }
</style>
