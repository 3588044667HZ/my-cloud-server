<template>
  <div class="drive">
    <header class="top">
      <el-button text @click="back"><el-icon><ArrowLeft /></el-icon> 云服务</el-button>
      <span class="page-title">云盘</span>
      <div class="top-right">
        <el-button text type="primary" @click="showTrash = !showTrash">
          <el-icon><Delete /></el-icon> 回收站
        </el-button>
      </div>
    </header>

    <main class="body">
      <!-- 盘选择 + 面包屑 -->
      <div class="nav">
        <el-select v-model="share" class="share-select" @change="onShareChange">
          <el-option v-for="s in shares" :key="s.name" :label="`${s.name}（${fmt(s.free)} 可用）`" :value="s.name" />
        </el-select>
        <div class="crumb">
          <a @click="cd('/')">/</a>
          <template v-for="(seg, i) in segments" :key="i">
            <span class="sep">/</span>
            <a @click="cd(upTo(i))">{{ seg }}</a>
          </template>
        </div>
      </div>

      <!-- 工具栏 -->
      <div class="toolbar">
        <el-upload :show-file-list="false" :auto-upload="false" multiple :on-change="onPick">
          <el-button type="primary" size="small">上传</el-button>
        </el-upload>
        <el-button size="small" @click="folderInput?.click()">上传文件夹</el-button>
        <input ref="folderInput" type="file" webkitdirectory multiple style="display:none" @change="onPickFolder" />
        <el-button size="small" @click="openMkdir">新建文件夹</el-button>
        <el-button size="small" @click="refresh">刷新</el-button>
      </div>

      <!-- 传输队列 -->
      <div v-if="transfers.length" class="tasks">
        <div v-for="t in transfers" :key="t.id" class="task">
          <span class="t-name">{{ t.type === 'up' ? '↑' : '↓' }} {{ t.name }}</span>
          <el-progress :percentage="t.progress" :stroke-width="8" />
        </div>
      </div>

      <!-- 回收站 -->
      <el-card v-if="showTrash" class="trash" shadow="never">
        <template #header>
          <div class="trash-head">
            <span>回收站</span>
            <el-button size="small" type="danger" plain @click="emptyTrash">清空</el-button>
          </div>
        </template>
        <el-table :data="trash" size="small" max-height="280">
          <el-table-column prop="origPath" label="原路径" />
          <el-table-column prop="deletedAt" label="删除时间" width="190" />
          <el-table-column label="操作" width="90">
            <template #default="{ row }">
              <el-button size="small" text type="primary" @click="restore(row)">还原</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <!-- 文件列表 -->
      <el-table :data="entries" v-loading="loading" @row-click="onRow" class="file-table">
        <el-table-column width="50">
          <template #default="{ row }">
            <el-checkbox v-model="row._sel" @click.stop @change="updateSel" />
          </template>
        </el-table-column>
        <el-table-column label="名称" min-width="260">
          <template #default="{ row }">
            <span class="entry">
              <el-icon v-if="row.isDir" class="ic-dir"><Folder /></el-icon>
              <img v-else-if="row.ext && ['jpg','jpeg','png','gif','webp'].includes(row.ext)"
                   :src="thumbUrl(row)" class="thumb" loading="lazy" />
              <el-icon v-else class="ic-file"><Document /></el-icon>
              <span class="name">{{ row.name }}</span>
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="size" label="大小" width="120">
          <template #default="{ row }">{{ row.isDir ? '—' : fmt(row.size) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="360" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.isDir" size="small" text type="primary" @click.stop="enter(row)">打开</el-button>
            <el-button v-if="row.isDir" size="small" text type="primary" @click.stop="downloadFolder(row)">下载</el-button>
            <el-button v-if="row.isDir" size="small" text type="primary" @click.stop="downloadFolderZip(row)">打包zip</el-button>
            <el-button v-else size="small" text type="primary" @click.stop="download(row)">下载</el-button>
            <el-button size="small" text type="primary" @click.stop="openRename(row)">重命名</el-button>
            <el-button size="small" text type="danger" @click.stop="del(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 批量操作条 -->
      <div v-if="selected.length" class="batch">
        <span>已选 {{ selected.length }} 项</span>
        <el-button size="small" @click="downloadSel">下载</el-button>
        <el-button size="small" type="danger" @click="delSel">删除</el-button>
        <el-button size="small" text @click="clearSel">取消</el-button>
      </div>
    </main>

    <!-- 新建文件夹 -->
    <el-dialog v-model="mkdirVisible" title="新建文件夹" width="380px">
      <el-input v-model="mkdirName" placeholder="文件夹名称" @keyup.enter="submitMkdir" />
      <template #footer>
        <el-button @click="mkdirVisible = false">取消</el-button>
        <el-button type="primary" @click="submitMkdir">确定</el-button>
      </template>
    </el-dialog>

    <!-- 重命名 -->
    <el-dialog v-model="renameVisible" title="重命名" width="380px">
      <el-input v-model="renameName" />
      <template #footer>
        <el-button @click="renameVisible = false">取消</el-button>
        <el-button type="primary" @click="submitRename">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, getToken } from '../shared/client'
import { chunkedUpload, fileMd5, fmtBytes } from '../shared/util'

const fmt = fmtBytes
const back = () => (window.location.href = '/')

const shares = ref([])
const share = ref('')
const path = ref('/')
const entries = ref([])
const loading = ref(false)
const showTrash = ref(false)
const trash = ref([])
const transfers = reactive([])
const selected = ref([])

const mkdirVisible = ref(false)
const mkdirName = ref('')
const renameVisible = ref(false)
const renameName = ref('')
const renameTarget = ref(null)
const folderInput = ref(null)

const segments = computed(() => path.value.split('/').filter(Boolean))

const upTo = (i) => '/' + segments.value.slice(0, i + 1).join('/')
const cd = (p) => { path.value = p; load() }
const enter = (row) => { path.value = joinPath(path.value, row.name); load() }

function joinPath(base, name) {
  return (base === '/' ? '/' : base + '/') + name
}

function onShareChange() {
  path.value = '/'
  load()
}

function thumbUrl(row) {
  return `/api/drive/thumb?share=${share.value}&path=${encodeURIComponent(joinPath(path.value, row.name))}`
}

async function load() {
  if (!share.value) return
  loading.value = true
  try {
    const r = await api.get('/api/drive/list', { share: share.value, path: path.value })
    if (r.code === 0) {
      entries.value = r.data.entries
      clearSel()
    } else {
      ElMessage.error(r.msg || '加载失败')
    }
  } finally {
    loading.value = false
  }
}

async function loadShares() {
  const r = await api.get('/api/drive/shares')
  if (r.code === 0) {
    shares.value = r.data
    if (r.data.length && !share.value) share.value = r.data[0].name
  }
}

async function loadTrash() {
  const r = await api.post('/api/drive/trash/list', { share: share.value })
  if (r.code === 0) trash.value = r.data
}

const refresh = () => { load(); loadTrash() }

function updateSel() {
  selected.value = entries.value.filter((e) => e._sel)
}
function clearSel() {
  entries.value.forEach((e) => (e._sel = false))
  selected.value = []
}

function onRow(row) {
  if (row.isDir) enter(row)
  else previewOrDownload(row)
}

async function previewOrDownload(row) {
  // 图片内嵌预览；其它直接下载
  if (['jpg', 'jpeg', 'png', 'gif', 'webp'].includes(row.ext)) {
    window.open(`/api/drive/download?share=${share.value}&path=${encodeURIComponent(joinPath(path.value, row.name))}`, '_blank')
  } else {
    download(row)
  }
}

async function download(row) {
  const src = `/api/drive/download?share=${share.value}&path=${encodeURIComponent(joinPath(path.value, row.name))}`
  const res = await api.fetchBinary(src)
  if (!res.ok) return
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = row.name
  a.click()
  URL.revokeObjectURL(url)
}

/* 打包下载整个文件夹（§6.5 zip=1 流式 ZIP） */
async function downloadFolderZip(row) {
  const src = `/api/drive/download?share=${share.value}&path=${encodeURIComponent(joinPath(path.value, row.name))}&zip=1`
  const res = await api.fetchBinary(src)
  if (!res.ok) return
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${row.name}.zip`
  a.click()
  URL.revokeObjectURL(url)
}

/* 下载整个文件夹（主方案，SPEC §7.3）：递归 list + 逐文件 Range 下载，
   File System Access API 落盘并按相对路径重建目录；传输面板显示总进度。 */
async function downloadFolder(row) {
  if (!window.showDirectoryPicker) {
    ElMessage.warning('当前浏览器不支持「保存到文件夹」，请改用「打包zip」')
    return
  }
  let targetDir
  try {
    targetDir = await window.showDirectoryPicker()
  } catch {
    return // 用户取消选择目录
  }
  const rootPath = joinPath(path.value, row.name)
  const task = reactive({ id: `${Date.now()}`, name: row.name, progress: 0, type: 'dir-dl', done: 0, total: 0 })
  transfers.push(task)
  try {
    task.total = await _countFiles(rootPath)
    if (task.total === 0) {
      ElMessage.info('文件夹为空')
      return
    }
    await _downloadDirRecursive(rootPath, targetDir, task)
    ElMessage.success(`已下载 ${task.done} 个文件`)
  } catch (e) {
    ElMessage.error(`下载失败: ${e.message || ''}`)
  } finally {
    const i = transfers.indexOf(task)
    if (i >= 0) transfers.splice(i, 1)
  }
}

async function _countFiles(p) {
  const r = await api.get('/api/drive/list', { share: share.value, path: p })
  if (r.code !== 0) return 0
  let n = 0
  for (const e of r.data.entries) {
    n += e.isDir ? await _countFiles(joinPath(p, e.name)) : 1
  }
  return n
}

async function _downloadDirRecursive(dirPath, dirHandle, task) {
  const r = await api.get('/api/drive/list', { share: share.value, path: dirPath })
  if (r.code !== 0) return
  for (const entry of r.data.entries) {
    if (entry.isDir) {
      const sub = await dirHandle.getDirectoryHandle(entry.name, { create: true })
      await _downloadDirRecursive(joinPath(dirPath, entry.name), sub, task)
    } else {
      const src = `/api/drive/download?share=${share.value}&path=${encodeURIComponent(joinPath(dirPath, entry.name))}`
      const res = await api.fetchBinary(src)
      if (!res.ok) throw new Error(`${entry.name} 下载失败`)
      const blob = await res.blob()
      const fh = await dirHandle.getFileHandle(entry.name, { create: true })
      const w = await fh.createWritable()
      await w.write(blob)
      await w.close()
      task.done += 1
      task.progress = task.total ? Math.round((task.done / task.total) * 100) : 100
    }
  }
}

async function downloadSel() {
  for (const row of selected.value) {
    if (!row.isDir) await download(row)
  }
}

async function del(row) {
  try {
    await ElMessageBox.confirm(`删除「${row.name}」？将移入回收站。`, '删除确认', { type: 'warning' })
  } catch { return }
  const r = await api.del('/api/drive/delete', { share: share.value, path: joinPath(path.value, row.name) })
  if (r.code === 0) {
    ElMessage.success('已移入回收站')
    refresh()
  } else ElMessage.error(r.msg || '删除失败')
}

async function delSel() {
  for (const row of selected.value) {
    await api.del('/api/drive/delete', { share: share.value, path: joinPath(path.value, row.name) })
  }
  ElMessage.success(`已删除 ${selected.value.length} 项`)
  refresh()
}

function openMkdir() {
  mkdirName.value = ''
  mkdirVisible.value = true
}

async function submitMkdir() {
  const name = mkdirName.value.trim()
  if (!name) return
  const r = await api.post('/api/drive/mkdir', { share: share.value, path: joinPath(path.value, name) })
  if (r.code === 0) {
    ElMessage.success('已创建')
    mkdirVisible.value = false
    load()
  } else ElMessage.error(r.msg || '创建失败')
}

function openRename(row) {
  renameTarget.value = row
  renameName.value = row.name
  renameVisible.value = true
}

async function submitRename() {
  const name = renameName.value.trim()
  if (!name || !renameTarget.value) return
  const from = joinPath(path.value, renameTarget.value.name)
  const to = joinPath(path.value, name)
  const r = await api.post('/api/drive/rename', { share: share.value, from, to })
  if (r.code === 0) {
    ElMessage.success('已重命名')
    renameVisible.value = false
    load()
  } else ElMessage.error(r.msg || '重命名失败')
}

async function onPick(file) {
  await uploadOne(file.raw, joinPath(path.value, file.raw.name))
}

/* 上传单个文件到指定目标路径（§6.5：path 含未存在子目录时服务端自动建父目录）。
   传入 aggregateTask 时并入文件夹整体进度，否则自建一条传输记录。 */
async function uploadOne(f, targetPath, aggregateTask) {
  const t = aggregateTask || reactive({
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 6)}`, name: f.name,
    progress: 0, type: 'up',
  })
  if (!aggregateTask) transfers.push(t)
  try {
    // 整文件 md5：判重秒传（服务端同内容直接完成）+ complete 终验
    const fileMd5Val = f.size <= 64 * 1024 * 1024 ? await fileMd5(f) : ''
    const result = await chunkedUpload({
      initUrl: '/api/drive/upload/init',
      chunkUrl: (uid, seq) => `/api/drive/upload/${uid}/${seq}`,
      completeUrl: (uid) => `/api/drive/upload/${uid}/complete`,
      file: f,
      md5: fileMd5Val,
      extraInit: { share: share.value, path: targetPath },
      onProgress: (p) => {
        if (aggregateTask) {
          const base = Math.round((aggregateTask.done / aggregateTask.total) * 100)
          aggregateTask.progress = Math.min(100, base + Math.round(p / aggregateTask.total))
        } else {
          t.progress = p
        }
      },
    })
    const skipped = !!(result && result.exists)
    if (aggregateTask) {
      aggregateTask.done += 1
      aggregateTask.progress = Math.round((aggregateTask.done / aggregateTask.total) * 100)
    } else {
      ElMessage.success(skipped ? `${f.name} 已存在（秒传跳过）` : `已上传 ${f.name}`)
    }
  } catch (e) {
    ElMessage.error(`${f.name} 上传失败: ${e.message || ''}`)
  } finally {
    if (!aggregateTask) {
      const i = transfers.indexOf(t)
      if (i >= 0) transfers.splice(i, 1)
    }
  }
}

/* 上传整个文件夹：逐文件带相对路径上传（服务端自动建父目录），传输面板显示整体进度 */
async function onPickFolder(e) {
  const files = Array.from(e.target.files || [])
  if (!files.length) return
  const first = files[0]
  const rootName = first.webkitRelativePath ? first.webkitRelativePath.split('/')[0] : ''
  const task = reactive({
    id: `${Date.now()}`, name: rootName || '文件夹', progress: 0, type: 'up', done: 0, total: files.length,
  })
  transfers.push(task)
  for (const f of files) {
    const relParts = (f.webkitRelativePath || f.name).split('/').slice(rootName ? 1 : 0)
    const target = joinPath(path.value, relParts.join('/'))
    await uploadOne(f, target, task)
  }
  const i = transfers.indexOf(task)
  if (i >= 0) transfers.splice(i, 1)
  ElMessage.success(`文件夹上传完成（${task.done}/${task.total}）`)
  load()
  e.target.value = ''
}

async function restore(row) {
  const r = await api.post('/api/drive/trash/restore', { id: row.id })
  if (r.code === 0) {
    ElMessage.success('已还原')
    loadTrash()
    load()
  } else ElMessage.error(r.msg || '还原失败')
}

async function emptyTrash() {
  try {
    await ElMessageBox.confirm('清空回收站将物理删除全部文件，确定？', '清空确认', { type: 'warning' })
  } catch { return }
  const r = await api.del('/api/drive/trash/empty', { share: share.value })
  if (r.code === 0) {
    ElMessage.success(`已清空 ${r.data.removed} 项`)
    loadTrash()
  }
}

onMounted(async () => {
  if (!getToken()) {
    window.location.href = '/portal/pair.html'
    return
  }
  await loadShares()
  await load()
  await loadTrash()
})
</script>

<style scoped>
.drive { min-height: 100vh; }
.top { height: 56px; display: flex; align-items: center; gap: 12px; padding: 0 20px; background: #fff; box-shadow: 0 1px 4px rgba(0,0,0,.06); }
.page-title { font-weight: 700; font-size: 17px; }
.top-right { margin-left: auto; }
.body { max-width: 1100px; margin: 0 auto; padding: 20px; }
.nav { display: flex; align-items: center; gap: 16px; margin-bottom: 16px; }
.share-select { width: 200px; }
.crumb { font-size: 15px; }
.crumb a { color: #409eff; cursor: pointer; }
.sep { color: #c0c4cc; margin: 0 4px; }
.toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
.tasks { margin-bottom: 12px; }
.task { display: flex; align-items: center; gap: 12px; margin-bottom: 6px; }
.t-name { width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.task .el-progress { flex: 1; }
.trash { margin-bottom: 16px; }
.trash-head { display: flex; align-items: center; justify-content: space-between; }
.entry { display: flex; align-items: center; gap: 8px; }
.ic-dir { color: #e6a23c; font-size: 20px; }
.ic-file { color: #909399; font-size: 20px; }
.thumb { width: 36px; height: 36px; object-fit: cover; border-radius: 4px; }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-table { background: #fff; border-radius: 8px; }
.batch {
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  background: #303133; color: #fff; border-radius: 8px; padding: 10px 16px;
  display: flex; align-items: center; gap: 12px; box-shadow: 0 6px 20px rgba(0,0,0,.3); z-index: 20;
}
</style>
