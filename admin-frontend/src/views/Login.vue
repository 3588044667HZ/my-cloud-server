<template>
  <div class="login-wrap">
    <el-card class="login-card" shadow="always">
      <h2 class="title">my-cloud-server 管理后台</h2>
      <p class="subtitle">设备配对 · 硬盘管理 · 系统状态</p>
      <el-form @submit.prevent="doLogin">
        <el-form-item>
          <el-input
            v-model="password"
            type="password"
            placeholder="管理密码"
            show-password
            size="large"
            @keyup.enter="doLogin"
          />
        </el-form-item>
        <el-button
          type="primary"
          size="large"
          class="login-btn"
          :loading="loading"
          @click="doLogin"
        >
          登录
        </el-button>
      </el-form>
      <p v-if="error" class="error">{{ error }}</p>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { loginAdmin } from '../api'

const router = useRouter()
const password = ref('')
const loading = ref(false)
const error = ref('')

async function doLogin() {
  if (!password.value) return
  loading.value = true
  error.value = ''
  try {
    const data = await loginAdmin(password.value)
    localStorage.setItem('admin_token', data.token)
    ElMessage.success('登录成功')
    router.push('/dashboard')
  } catch (e) {
    error.value = e.response?.data?.msg || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrap {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}
.login-card {
  width: 380px;
  padding: 12px 8px;
}
.title {
  margin: 0 0 4px;
  text-align: center;
}
.subtitle {
  margin: 0 0 20px;
  text-align: center;
  color: #909399;
  font-size: 13px;
}
.login-btn {
  width: 100%;
}
.error {
  color: #f56c6c;
  text-align: center;
  margin-top: 12px;
}
</style>
