<template>
  <div>
    <!-- 云盘开关 -->
    <el-card shadow="hover" class="panel">
      <div class="switch-row">
        <div>
          <div class="switch-title">云盘功能</div>
          <div class="hint">关闭后全部客户端无法访问云盘（设备仍可正常使用相册/笔记）</div>
        </div>
        <el-switch v-model="enabled" @change="toggleEnabled" :loading="enabledLoading" />
      </div>
    </el-card>

    <!-- 硬盘列表 -->
    <el-card shadow="hover" class="panel">
      <template #header>
        <div class="card-head">
          <span>共享目录 / 硬盘（{{ shares.length }}）</span>
          <el-button type="primary" @click="openAdd">新增硬盘</el-button>
        </div>
      </template>
      <el-table :data="shares" v-loading="loading" stripe>
        <el-table-column prop="name" label="盘名" width="130">
          <template #default="{ row }">
            <b>{{ row.name }}</b>
            <el-tag v-if="!row.enabled" size="small" type="info" style="margin-left: 6px">停用</el-tag>
            <el-tag v-if="!row.exists" size="small" type="danger" style="margin-left: 6px">路径失效</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="path" label="本地路径" min-width="220" show-overflow-tooltip />
        <el-table-column prop="comment" label="备注" width="120" />
        <el-table-column label="来源" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="sourceType(row.source)">{{ sourceLabel(row.source) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="容量" min-width="200">
          <template #default="{ row }">
            <div v-if="row.total" class="space-cell">
              <el-progress
                :percentage="percent(row)"
                :stroke-width="10"
                :show-text="false"
                :color="spaceColor(row)"
              />
              <span class="space-text">
                已用 {{ fmt(row.used) }} / 共 {{ fmt(row.total) }}，剩余 {{ fmt(row.free) }}
              </span>
            </div>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button
              v-if="row.source === 'db'"
              size="small"
              text
              type="danger"
              @click="remove(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新增硬盘 -->
    <el-dialog v-model="addVisible" title="新增硬盘共享" width="480px">
      <el-form label-width="90px">
        <el-form-item label="盘名" required>
          <el-input v-model="form.name" placeholder="如 DATA / MEDIA（字母数字-_）" maxlength="32" />
        </el-form-item>
        <el-form-item label="本地路径" required>
          <el-input v-model="form.path" placeholder="如 D:/photos 或 E:/data（需存在）" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.comment" placeholder="可选" maxlength="64" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitAdd">确定</el-button>
      </template>
    </el-dialog>

    <!-- 编辑硬盘 -->
    <el-dialog v-model="editVisible" title="编辑硬盘共享" width="480px">
      <el-form label-width="90px">
        <el-form-item label="盘名">
          <el-input :model-value="editForm.name" disabled />
        </el-form-item>
        <el-form-item label="本地路径">
          <el-input v-model="editForm.path" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="editForm.comment" maxlength="64" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="editForm.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  addShare, deleteShare, getDriveEnabled, getShares, setDriveEnabled, updateShare,
} from '../api'

const enabled = ref(false)
const enabledLoading = ref(false)
const loading = ref(false)
const saving = ref(false)
const shares = ref([])

const addVisible = ref(false)
const editVisible = ref(false)
const form = ref({ name: '', path: '', comment: '' })
const editForm = ref({ name: '', path: '', comment: '', enabled: true })

const GB = 1024 * 1024 * 1024

function fmt(n) {
  if (n == null) return '-'
  const v = Number(n)
  if (v >= GB) return `${(v / GB).toFixed(1)} GB`
  if (v >= 1024 * 1024) return `${(v / 1024 / 1024).toFixed(1)} MB`
  return `${(v / 1024).toFixed(0)} KB`
}

function percent(row) {
  if (!row.total) return 0
  return Math.round((Number(row.used) / Number(row.total)) * 100)
}

function spaceColor(row) {
  const p = percent(row)
  return p >= 90 ? '#f56c6c' : p >= 75 ? '#e6a23c' : '#67c23a'
}

function sourceType(s) {
  return { db: 'primary', config: 'warning', registry: 'info' }[s] || 'info'
}

function sourceLabel(s) {
  return { db: '后台', config: '配置', registry: 'SMB' }[s] || s
}

async function refresh() {
  loading.value = true
  try {
    ;[shares.value, enabled.value] = await Promise.all([getShares(), getDriveEnabled()])
  } catch (e) {
    ElMessage.error(e.response?.data?.msg || '加载失败')
  } finally {
    loading.value = false
  }
}

async function toggleEnabled(val) {
  enabledLoading.value = true
  try {
    await setDriveEnabled(val)
    ElMessage.success(val ? '云盘已开启' : '云盘已关闭')
  } catch (e) {
    enabled.value = !val
    ElMessage.error(e.response?.data?.msg || '操作失败')
  } finally {
    enabledLoading.value = false
  }
}

function openAdd() {
  form.value = { name: '', path: '', comment: '' }
  addVisible.value = true
}

async function submitAdd() {
  if (!form.value.name || !form.value.path) {
    ElMessage.warning('盘名与路径必填')
    return
  }
  saving.value = true
  try {
    await addShare({ ...form.value })
    ElMessage.success('已新增硬盘')
    addVisible.value = false
    refresh()
  } catch (e) {
    ElMessage.error(e.response?.data?.msg || '新增失败')
  } finally {
    saving.value = false
  }
}

function openEdit(row) {
  editForm.value = {
    name: row.name,
    path: row.path,
    comment: row.comment,
    enabled: row.enabled,
  }
  editVisible.value = true
}

async function submitEdit() {
  saving.value = true
  try {
    await updateShare(editForm.value.name, {
      path: editForm.value.path,
      comment: editForm.value.comment,
      enabled: editForm.value.enabled,
    })
    ElMessage.success('已保存')
    editVisible.value = false
    refresh()
  } catch (e) {
    ElMessage.error(e.response?.data?.msg || '保存失败')
  } finally {
    saving.value = false
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(
      `删除硬盘共享「${row.name}」？不影响磁盘上的文件，但该盘回收站条目将无法还原。`,
      '删除确认',
      { type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await deleteShare(row.name)
    ElMessage.success('已删除')
    refresh()
  } catch (e) {
    ElMessage.error(e.response?.data?.msg || '删除失败')
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
.switch-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.switch-title {
  font-weight: 600;
}
.hint {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
}
.space-cell {
  width: 100%;
}
.space-text {
  font-size: 12px;
  color: #606266;
}
.muted {
  color: #c0c4cc;
}
</style>
