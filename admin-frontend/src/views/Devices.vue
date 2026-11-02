<template>
  <div>
    <!-- 配对码 -->
    <el-card shadow="hover" class="panel">
      <template #header>
        <div class="card-head">
          <span>生成配对码</span>
          <span class="hint">600 秒内一次性有效，用于客户端首次配对</span>
        </div>
      </template>
      <div class="pair-row">
        <el-button type="primary" :loading="genLoading" @click="generate">生成配对码</el-button>
        <el-tag v-if="pairCode" size="large" type="success" class="code-tag">{{ pairCode }}</el-tag>
        <el-button v-if="pairCode" text type="primary" @click="copyCode">复制</el-button>
      </div>
    </el-card>

    <!-- 设备列表 -->
    <el-card shadow="hover" class="panel">
      <template #header>
        <div class="card-head">
          <span>设备列表</span>
          <el-button text type="primary" @click="refresh">刷新</el-button>
        </div>
      </template>
      <el-table :data="devices" v-loading="loading" stripe>
        <el-table-column prop="name" label="设备名" width="160">
          <template #default="{ row }">
            <span>{{ row.name || '(未命名)' }}</span>
            <el-tag v-if="row.revokedAt" type="danger" size="small">已撤销</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="deviceId" label="设备 ID" min-width="200" show-overflow-tooltip />
        <el-table-column prop="createdAt" label="配对时间" width="180" />
        <el-table-column prop="lastActive" label="最近活跃" width="180" />
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="!row.revokedAt"
              size="small"
              text
              type="primary"
              @click="openRename(row)"
            >
              重命名
            </el-button>
            <el-button
              v-if="!row.revokedAt"
              size="small"
              text
              type="danger"
              @click="revoke(row)"
            >
              撤销
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 孤儿设备 -->
    <el-card shadow="hover" class="panel">
      <template #header>
        <div class="card-head">
          <span>孤儿设备（{{ orphans.length }} 台，last_active 超 30 天）</span>
          <el-button
            v-if="orphans.length"
            size="small"
            type="warning"
            :loading="cleanupLoading"
            @click="cleanup"
          >
            一键回收
          </el-button>
        </div>
      </template>
      <el-empty v-if="!orphans.length" description="暂无孤儿设备" :image-size="60" />
      <el-table v-else :data="orphans" size="small">
        <el-table-column prop="name" label="设备名" width="160">
          <template #default="{ row }">{{ row.name || '(未命名)' }}</template>
        </el-table-column>
        <el-table-column prop="deviceId" label="设备 ID" min-width="200" show-overflow-tooltip />
        <el-table-column prop="lastActive" label="最近活跃" width="200" />
      </el-table>
    </el-card>

    <!-- 重命名对话框 -->
    <el-dialog v-model="renameVisible" title="重命名设备" width="420px">
      <el-input v-model="renameName" placeholder="设备名" maxlength="32" />
      <template #footer>
        <el-button @click="renameVisible = false">取消</el-button>
        <el-button type="primary" :loading="renameLoading" @click="doRename">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  cleanupOrphans, genPairCode, getDevices, getOrphans, renameDevice, revokeDevice,
} from '../api'

const loading = ref(false)
const genLoading = ref(false)
const cleanupLoading = ref(false)
const renameLoading = ref(false)
const devices = ref([])
const orphans = ref([])
const pairCode = ref('')

const renameVisible = ref(false)
const renameName = ref('')
const renameTarget = ref(null)

async function refresh() {
  loading.value = true
  try {
    ;[devices.value, orphans.value] = await Promise.all([getDevices(), getOrphans()])
  } catch (e) {
    ElMessage.error(e.response?.data?.msg || '加载失败')
  } finally {
    loading.value = false
  }
}

async function generate() {
  genLoading.value = true
  try {
    const r = await genPairCode()
    pairCode.value = r.code
  } catch (e) {
    ElMessage.error(e.response?.data?.msg || '生成失败')
  } finally {
    genLoading.value = false
  }
}

function copyCode() {
  navigator.clipboard?.writeText(pairCode.value)
  ElMessage.success('已复制')
}

function openRename(row) {
  renameTarget.value = row
  renameName.value = row.name || ''
  renameVisible.value = true
}

async function doRename() {
  if (!renameTarget.value) return
  renameLoading.value = true
  try {
    await renameDevice(renameTarget.value.deviceId, renameName.value.trim())
    ElMessage.success('已重命名')
    renameVisible.value = false
    refresh()
  } catch (e) {
    ElMessage.error(e.response?.data?.msg || '重命名失败')
  } finally {
    renameLoading.value = false
  }
}

async function revoke(row) {
  try {
    await ElMessageBox.confirm(
      `撤销设备「${row.name || row.deviceId}」后其 token 立即失效，需重新配对。`,
      '撤销确认',
      { type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await revokeDevice(row.deviceId)
    ElMessage.success('已撤销')
    refresh()
  } catch (e) {
    ElMessage.error(e.response?.data?.msg || '撤销失败')
  }
}

async function cleanup() {
  try {
    await ElMessageBox.confirm('确定回收全部孤儿设备？撤销后需重新配对。', '回收确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  cleanupLoading.value = true
  try {
    const r = await cleanupOrphans()
    ElMessage.success(`已回收 ${r.revoked} 台`)
    refresh()
  } catch (e) {
    ElMessage.error(e.response?.data?.msg || '清理失败')
  } finally {
    cleanupLoading.value = false
  }
}

onMounted(refresh)
</script>

<style scoped>
.panel {
  margin-bottom: 16px;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.hint {
  font-size: 12px;
  color: #909399;
}
.pair-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.code-tag {
  font-size: 18px;
  letter-spacing: 2px;
}
</style>
