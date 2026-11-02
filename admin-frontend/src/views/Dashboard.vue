<template>
  <div>
    <el-row :gutter="16">
      <el-col :span="8">
        <el-card shadow="hover">
          <div class="stat">
            <div class="stat-num">{{ stats.deviceCount ?? '-' }}</div>
            <div class="stat-label">设备总数</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <div class="stat">
            <div class="stat-num stat-ok">{{ stats.activeDeviceCount ?? '-' }}</div>
            <div class="stat-label">在线/有效设备</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <div class="stat">
            <div class="stat-num stat-warn">{{ stats.orphanCount ?? '-' }}</div>
            <div class="stat-label">孤儿设备（30 天未活跃）</div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" class="row2">
      <el-col :span="8">
        <el-card shadow="hover">
          <div class="stat">
            <div class="stat-num">{{ stats.noteCount ?? '-' }}</div>
            <div class="stat-label">笔记总数</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <div class="stat">
            <div class="stat-num stat-warn">{{ stats.trashCount ?? '-' }}</div>
            <div class="stat-label">回收站笔记</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <div class="stat">
            <div class="stat-num stat-ok">{{ stats.photoUploads ?? '-' }}</div>
            <div class="stat-label">照片上传数</div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="hover" class="panel">
      <template #header>
        <div class="card-head">
          <span>快速操作</span>
        </div>
      </template>
      <div class="quick-actions">
        <el-button type="primary" @click="$router.push('/devices')">生成配对码</el-button>
        <el-button type="success" @click="$router.push('/drives')">硬盘管理</el-button>
        <el-button type="warning" @click="goCleanup">孤儿设备清理</el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { cleanupOrphans, getStats } from '../api'

const router = useRouter()
const stats = ref({})

async function refresh() {
  try {
    stats.value = await getStats()
  } catch (e) {
    ElMessage.error(e.response?.data?.msg || '获取统计失败')
  }
}

async function goCleanup() {
  try {
    const r = await cleanupOrphans()
    ElMessage.success(`已回收 ${r.revoked} 台孤儿设备`)
    refresh()
  } catch (e) {
    ElMessage.error(e.response?.data?.msg || '清理失败')
  }
}

onMounted(refresh)
</script>

<style scoped>
.stat {
  text-align: center;
  padding: 12px 0;
}
.stat-num {
  font-size: 34px;
  font-weight: 700;
  color: #409eff;
}
.stat-ok {
  color: #67c23a;
}
.stat-warn {
  color: #e6a23c;
}
.stat-label {
  margin-top: 8px;
  color: #909399;
}
.panel {
  margin-top: 16px;
}
.row2 {
  margin-top: 16px;
}
.quick-actions {
  display: flex;
  gap: 12px;
}
</style>
