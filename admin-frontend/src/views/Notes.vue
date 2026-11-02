<template>
  <div>
    <el-card shadow="hover">
      <template #header>
        <div class="card-head">
          <span>笔记管理</span>
          <div class="filters">
            <el-input v-model="keyword" placeholder="标题/内容搜索" clearable style="width: 220px"
                      @keyup.enter="refresh" @clear="refresh" />
            <el-switch v-model="includeTrash" active-text="含回收站" @change="refresh" />
            <el-button type="primary" @click="refresh">查询</el-button>
          </div>
        </div>
      </template>

      <el-table :data="notes" v-loading="loading" stripe>
        <el-table-column prop="title" label="标题" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span>{{ row.title || '(无标题)' }}</span>
            <el-tag v-if="row.deletedAt" type="danger" size="small" style="margin-left: 6px">回收站</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="content" label="内容" min-width="280" show-overflow-tooltip>
          <template #default="{ row }">{{ excerpt(row.content) }}</template>
        </el-table-column>
        <el-table-column prop="username" label="用户" width="120" />
        <el-table-column prop="version" label="版本" width="70" align="center" />
        <el-table-column prop="updatedAt" label="更新时间" width="180" />
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          layout="prev, pager, next, total"
          :total="total"
          :page-size="pageSize"
          v-model:current-page="page"
          @current-change="refresh"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { deleteAdminNote, getAdminNotes } from '../api'

const notes = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 20
const keyword = ref('')
const includeTrash = ref(false)

function excerpt(html) {
  const text = (html || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
  return text.slice(0, 80)
}

async function refresh() {
  loading.value = true
  try {
    const r = await getAdminNotes({
      page: page.value, size: pageSize,
      keyword: keyword.value || undefined,
      includeTrash: includeTrash.value || undefined,
    })
    notes.value = r.data
    total.value = r.pagination.total
  } catch (e) {
    ElMessage.error(e.response?.data?.msg || '加载失败')
  } finally {
    loading.value = false
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(
      `强制删除笔记「${row.title || row.id}」？将同时清理附件与冷备，不可恢复。`,
      '删除确认', { type: 'warning' }
    )
  } catch { return }
  try {
    await deleteAdminNote(row.id)
    ElMessage.success('已删除')
    refresh()
  } catch (e) {
    ElMessage.error(e.response?.data?.msg || '删除失败')
  }
}

onMounted(refresh)
</script>

<style scoped>
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.filters {
  display: flex;
  align-items: center;
  gap: 12px;
}
.pager {
  margin-top: 16px;
  display: flex;
  justify-content: center;
}
</style>
