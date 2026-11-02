<template>
  <div class="album">
    <header class="top">
      <el-button text @click="back"><el-icon><ArrowLeft /></el-icon> 云服务</el-button>
      <span class="page-title">相册</span>
      <div class="top-right">
        <el-tag v-if="space.total" size="small">
          已用 {{ fmt(space.used) }} / {{ fmt(space.total) }}
        </el-tag>
        <el-button text type="warning" @click="toggleTrash">
          <el-icon><Delete /></el-icon> 回收站
        </el-button>
        <el-upload
          v-if="!showTrash"
          :show-file-list="false"
          :auto-upload="false"
          accept="image/*,video/*"
          multiple
          :on-change="onPick"
        >
          <el-button type="primary" :disabled="paused">上传</el-button>
        </el-upload>
      </div>
    </header>

    <main class="body">
      <!-- 回收站（SPEC-CLOUD-RECYCLE-RECOVERY §1.3） -->
      <div v-if="showTrash" class="trash-panel">
        <el-table :data="trash" v-loading="trashLoading" size="small">
          <el-table-column label="缩略图" width="90">
            <template #default="{ row }">
              <img :src="row.thumbUrl" class="trash-thumb" />
            </template>
          </el-table-column>
          <el-table-column prop="filename" label="文件名" min-width="160" show-overflow-tooltip />
          <el-table-column prop="deletedAt" label="删除时间" width="180" />
          <el-table-column label="剩余天数" width="110">
            <template #default="{ row }">{{ row.remainDays }} 天</template>
          </el-table-column>
          <el-table-column label="操作" width="150">
            <template #default="{ row }">
              <el-button size="small" text type="primary" @click="restorePhoto(row)">恢复</el-button>
              <el-button size="small" text type="danger" @click="permanent(row)">彻底删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="trash-foot">
          <el-button size="small" type="danger" plain :disabled="!trash.length" @click="emptyTrash">
            清空回收站
          </el-button>
        </div>
      </div>

      <template v-else>
      <p v-if="paused" class="warn">磁盘空间不足（&lt;10GB），原图上传已暂停</p>

      <!-- 传输进度 -->
      <div v-if="uploading.length" class="tasks">
        <div v-for="t in uploading" :key="t.id" class="task">
          <span class="t-name">{{ t.name }}</span>
          <el-progress :percentage="t.progress" :stroke-width="8" />
        </div>
      </div>

      <el-empty v-if="!photos.length && !loading" description="暂无照片，点击右上角上传" />

      <div v-else class="grid">
        <div v-for="p in photos" :key="p.id" class="photo" @click="preview(p)">
          <img :src="p.thumbUrl" loading="lazy" :alt="p.filename" />
          <el-icon v-if="p.deletedAt" class="badge"><CircleClose /></el-icon>
        </div>
      </div>

      <div class="pager">
        <el-pagination
          layout="prev, pager, next, total"
          :total="total"
          :page-size="pageSize"
          v-model:current-page="page"
          @current-change="load"
        />
      </div>
      </template>
    </main>

    <!-- 预览 -->
    <el-dialog v-model="previewVisible" :title="current?.filename || '预览'" width="auto">
      <div class="preview-body">
        <img v-if="current" :src="current.origUrl" class="preview-img" />
      </div>
      <template #footer>
        <el-button @click="remove(current)" type="danger" plain>删除</el-button>
        <el-button type="primary" @click="downloadOrig(current)">下载原图</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, getToken } from '../shared/client'
import { chunkedUpload, fileMd5, fmtBytes, imageSize } from '../shared/util'

const photos = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 30
const paused = ref(false)
const space = reactive({ total: 0, used: 0, free: 0 })
const previewVisible = ref(false)
const current = ref(null)
const uploading = reactive([])
const showTrash = ref(false)
const trash = ref([])
const trashLoading = ref(false)
const fmt = fmtBytes

const back = () => (window.location.href = '/')

function toggleTrash() {
  showTrash.value = !showTrash.value
  if (showTrash.value) loadTrash()
}

async function loadTrash() {
  trashLoading.value = true
  try {
    const r = await api.get('/api/photos/trash', { page: 1, size: 200 })
    if (r.code === 0) trash.value = r.data
  } finally {
    trashLoading.value = false
  }
}

async function restorePhoto(row) {
  const r = await api.post('/api/photos/trash/restore', { id: row.id })
  if (r.code === 0) {
    ElMessage.success('已恢复')
    loadTrash(); load()
  } else ElMessage.error(r.msg || '恢复失败')
}

async function permanent(row) {
  try {
    await ElMessageBox.confirm('该照片将被彻底删除，删除后不可恢复。确定？', '彻底删除', {
      type: 'warning',
    })
  } catch { return }
  const r = await api.del(`/api/photos/trash/${row.id}`)
  if (r.code === 0) {
    ElMessage.success('已彻底删除')
    loadTrash()
  }
}

async function emptyTrash() {
  try {
    await ElMessageBox.confirm('清空回收站将彻底删除其中全部照片，不可恢复。确定？', '清空回收站', {
      type: 'warning',
    })
  } catch { return }
  const r = await api.del('/api/photos/trash/empty')
  if (r.code === 0) {
    ElMessage.success(`已清空 ${r.data.removed} 项`)
    loadTrash(); load()
  }
}

async function load() {
  loading.value = true
  try {
    const r = await api.get('/api/photos/list', { page: page.value, size: pageSize })
    if (r.code === 0) {
      photos.value = r.data
      total.value = r.pagination.total
    }
  } finally {
    loading.value = false
  }
}

async function loadSpace() {
  const r = await api.get('/api/space')
  if (r.code === 0) {
    Object.assign(space, r.data)
    paused.value = !!r.data.paused
  }
}

function preview(p) {
  current.value = p
  previewVisible.value = true
}

async function downloadOrig(p) {
  const res = await api.fetchBinary(p.origUrl)
  if (!res.ok) return
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = p.filename || p.id
  a.click()
  URL.revokeObjectURL(url)
}

async function remove(p) {
  try {
    const r = await api.del(`/api/photos/${p.id}`)
    if (r.code === 0) {
      ElMessage.success('已删除')
      previewVisible.value = false
      load()
    }
  } catch (e) {
    ElMessage.error(e.message || '删除失败')
  }
}

async function onPick(file) {
  const f = file.raw
  const task = reactive({ id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, name: f.name, progress: 0 })
  uploading.push(task)
  try {
    const md5 = await fileMd5(f)
    const { width, height } = await imageSize(f)
    const reg = await api.post('/api/photos/register', {
      clientId: task.id,
      mediaId: '',
      filename: f.name,
      width, height, size: f.size, md5,
    })
    if (reg.code !== 0) throw new Error(reg.msg)
    const pid = reg.data.id
    await chunkedUpload({
      initUrl: '/api/photos/upload/init',
      chunkUrl: (uid, seq) => `/api/photos/upload/${uid}/${seq}`,
      completeUrl: (uid) => `/api/photos/upload/${uid}/complete`,
      file: f,
      md5,
      extraInit: { photoId: pid },
      onProgress: (p) => (task.progress = p),
    })
    ElMessage.success(`已上传 ${f.name}`)
    load()
    loadSpace()
  } catch (e) {
    ElMessage.error(`${f.name} 上传失败: ${e.message || ''}`)
  } finally {
    const i = uploading.indexOf(task)
    if (i >= 0) uploading.splice(i, 1)
  }
}

onMounted(() => {
  if (!getToken()) {
    window.location.href = '/portal/pair.html'
    return
  }
  load()
  loadSpace()
})
</script>

<style scoped>
.album { min-height: 100vh; }
.top {
  height: 56px; display: flex; align-items: center; gap: 12px;
  padding: 0 20px; background: #fff; box-shadow: 0 1px 4px rgba(0,0,0,.06);
}
.page-title { font-weight: 700; font-size: 17px; }
.top-right { margin-left: auto; display: flex; align-items: center; gap: 12px; }
.body { max-width: 1100px; margin: 0 auto; padding: 20px; }
.warn { color: #e6a23c; font-size: 13px; }
.tasks { margin-bottom: 16px; }
.task { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
.t-name { width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.task .el-progress { flex: 1; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 10px; }
.photo {
  position: relative; aspect-ratio: 1; border-radius: 8px; overflow: hidden;
  cursor: pointer; background: #eef1f5;
}
.photo img { width: 100%; height: 100%; object-fit: cover; transition: transform .2s; }
.photo:hover img { transform: scale(1.05); }
.badge { position: absolute; top: 6px; right: 6px; color: #f56c6c; background: rgba(255,255,255,.85); border-radius: 50%; }
.pager { margin-top: 20px; display: flex; justify-content: center; }
.preview-body { display: flex; align-items: center; justify-content: center; }
.preview-img { max-width: 80vw; max-height: 70vh; border-radius: 8px; }
.trash-panel { background: #fff; border-radius: 8px; padding: 8px; }
.trash-thumb { width: 48px; height: 48px; object-fit: cover; border-radius: 4px; }
.trash-foot { margin-top: 12px; text-align: right; }
</style>
