# 云服务 Web 前端（portal · 相册 · 云盘）设计

> 状态：**已实现**（portal/album/drive 全功能可用）；本文档同步现状，剩余项见 §9
> 技术栈：**Vue3 + Vite + Element Plus**（MPA：portal/pair/album/drive 四个入口，纯浏览器）
> 形态：同百度网盘网页版——单页应用 + 上传下载 + 文件管理，全在浏览器
> 部署：my-cloud-server 静态页（与后端同源，共享登录态）
> 关联：`UNIFIED_DEVICE_TOKEN_AUTH_SPEC.md`、`ARCHITECTURE.md`
> 工程：`web/`（Vite MPA）→ 构建产物 `static/web/`（`scripts/deploy.ps1` 第 5 步）

## 1. 定位与路由

PC 端聚合云服务 = **纯 Web 前端**，浏览器打开即用，无桌面壳：

```
my-cloud-server 静态页（同源）
├── /               聚合页（portal，SPEC 调整：聚合页在 index，不再直接分发笔记）
├── /portal         聚合页别名（配对页入口 /portal/pair.html）
├── /web/album      相册浏览（云端照片网格/预览/上传/删除/下载）
├── /web/drive      云盘文件管理（盘切换/面包屑/上传下载/回收站）
└── /web/note       笔记（复用 open-note-frontend，仅经聚合页进入，UA 分发桌面/移动）
```

- **聚合页在 index**：`GET /` 返回 portal.html；笔记不再从根路径直达，只能经聚合页「便签」宫格进入 `/web/note`
- **保留 UA 分发**：`/web/note` 与笔记资源 `/assets/*` 按 UA 分发 `static/desktop`↔`static/mobile`（沿用 mock-server 语义）
- 未知路径一律 404（不提供任意路径的 SPA 兜底）
- 一份 Vue3 代码提供相册/云盘 UI；portal 是聚合入口。Android 端保持原生（不复用此 Web 代码）

## 2. 认证（复用设备 token，同源无需额外配置）✅ 已实现

- 浏览器访问 my-cloud-server 静态页 = **同源**，localStorage `device_token` 共享
- 流程：无 token → `/portal/pair.html` 配对 → 存 token → 回 `/` 聚合页
- 请求带 `Authorization: Bearer <device_token>`；401（`10401`）→ 清 token → 跳配对页（防死循环）
- 三个模块（portal/album/drive）共用同一登录态；跳转不传参
- 实现：`web/src/shared/client.js`（`request()` 统一封装、`redirectToPair()`、`pairDevice()`）

## 3. 目录结构（实际实现）

```
web/                          # Vite MPA 工程根
├── package.json · vite.config.js   # base=/web/，多入口，构建输出 ../static/web
├── portal.html · pair.html · album.html · drive.html   # 四个入口
├── portal_apps.json          # 应用宫格配置（/api/portal/apps 读取，缺省兜底）
└── src/
    ├── shared/               # 跨模块共用
    │   ├── client.js         # fetch 封装：Bearer 注入、401 拦截、pairDevice
    │   ├── util.js           # 分片上传器/下载器、文件 md5、图片尺寸、字节格式化
    │   └── base.css
    ├── portal/
    │   ├── main.js · pair_main.js
    │   ├── PortalHome.vue    # 欢迎区 + 应用宫格（6 列，Emoji/渐变兜底图标）
    │   └── PairPage.vue      # 配对守卫页（/portal/pair.html）
    ├── album/
    │   ├── main.js
    │   └── AlbumHome.vue     # 网格/预览/上传/删除/分页/磁盘余量
    └── drive/
        ├── main.js
        └── DriveHome.vue     # 盘切换/面包屑/文件列表/上传下载/新建/重命名/删除/回收站/批量
```

## 4. 登录态与 API 客户端（shared/client.js）✅ 已实现

```js
const TOKEN_KEY = 'device_token'
async function request(path, { method, body, headers, binary }) {
  // Bearer 注入；401 → clearSession() + redirectToPair()（/portal/pair.html）
  // 非二进制请求带 Content-Type: application/json，解析 { code, msg, data }
  // binary 走原始 Response（分片 PUT / Range 下载）
}
```

- 同源请求直接相对路径，无需配置 server_url
- 上传/下载（二进制）走独立 fetch（不设 `Content-Type: application/json`）

## 5. 导流首页（portal）✅ 已实现

仿 OPPO 云空间：欢迎区 + 我的应用宫格（6 列×N 行）。

- 宫格数据：`GET /api/portal/apps`（设备鉴权，服务端读 `web/portal_apps.json`，缺省列表兜底）
- 图标：配置 `icon` 字段为空时前端按应用 id 渲染 Emoji + 渐变底
- 点击 `location.href = item.url`（同源，登录态共享）；无 token 自动跳配对页

## 6. 相册（/web/album）✅ 已实现

### 6.1 页面

顶栏（返回聚合页/标题/磁盘余量/上传）→ 传输进度条 → 照片网格（3~N 列，缩略图懒加载）→ 分页 → 预览弹窗（删除/下载原图）。

### 6.2 接口对接（`/api/photos/*`）

| 动作 | 接口 | 状态 |
|---|---|---|
| 列照片（分页） | `GET /api/photos/list?page=&size=` | ✅ 服务端已补 |
| 缩略图 | `GET /api/photos/{id}/download?size=thumb`（懒加载，匿名可读） | ✅ |
| 原图 | `GET /api/photos/{id}/download`（预览/下载） | ✅ |
| 上传 | `register → upload/init → {seq} → complete`（分片） | ✅ |
| 删除 | `DELETE /api/photos/{id}`（tombstone） | ✅ |
| 磁盘余量 | `GET /api/space`（上传前检查，低余量暂停原图） | ✅ |

### 6.3 上传（分片，前端）✅

`读文件 md5（纯 JS 增量，兼容 HTTP 局域网/非安全上下文，不用 crypto.subtle）→ register{clientId,md5,...} → upload/init{photoId,md5} → 逐片 PUT → complete`；进度实时显示。

### 6.4 下载（Range 流式）✅

`fetch(url, { headers: { Range } })` → 206 流式；小文件直接 `<a download>` 浏览器原生下载。断点续传（IndexedDB / File System Access API）列为后续项（§8.2）。

## 7. 云盘（/web/drive）✅ 已实现

### 7.1 页面

顶栏（返回聚合页/标题/回收站入口）→ 盘选择（显示余量）+ 面包屑 → 工具栏（上传/上传文件夹/新建文件夹/刷新）→ 传输队列（含文件夹上传/下载整体进度）→ 文件列表（图片缩略图、文件夹/文件混排、勾选多选）→ 批量操作条（下载/删除/取消）→ 回收站（还原/清空）。

### 7.2 接口对接（`/api/drive/*`）

| 动作 | 接口 | 状态 |
|---|---|---|
| 盘列表（余量） | `GET /api/drive/shares` | ✅ |
| 列目录 | `GET /api/drive/list?share=X&path=` | ✅ |
| 上传 | `POST /api/drive/upload/init → PUT /{uploadId}/{seq} → complete`（分片） | ✅ |
| **上传文件夹** | `<input type=file webkitdirectory>` 遍历 → 逐文件带相对路径上传（服务端自动建父目录） | ✅ |
| 下载 | `GET /api/drive/download?share=X&path=`（Range 流式） | ✅ |
| **下载文件夹** | 主方案：递归 `list` + 逐文件 Range 下载，File System Access API 落盘重建目录；可选 `GET /download?path=/folder&zip=1` 流式 ZIP | ✅ |
| 新建/重命名/删除 | `POST /api/drive/mkdir|rename` · `DELETE /api/drive/delete` | ✅ |
| 回收站 | `POST /api/drive/trash/list|restore` · `DELETE /api/drive/trash/empty` | ✅ |
| 缩略图 | `GET /api/drive/thumb?share=X&path=`（匿名可读，供 `<img>`） | ✅ |

### 7.3 交互规范 ✅

面包屑 + 盘切换（下拉显示余量）、多选批量操作、删除进回收站、图片 `<img>` 内嵌预览（download/thumb 已改为**可选鉴权**：匿名可读、带 token 必校验）、大文件 Range 下载。

**文件夹操作** ✅：
- **下载文件夹（主方案）**：文件夹项「下载」→ `window.showDirectoryPicker()` 选择落盘目录 → 递归 `GET /api/drive/list` → 逐文件 `GET /api/drive/download`（Range 流式）→ File System Access API **按相对路径重建目录**写入；传输面板显示总进度（已下载数/总数）。**服务端零新增**。浏览器不支持 FS Access API 时提示改用「打包zip」。
- **打包下载（可选）**：文件夹项「打包zip」→ `GET /download?zip=1` 流式 ZIP（适合分享/导出）。
- **上传文件夹**：工具栏「上传文件夹」（`webkitdirectory`）→ 遍历文件，`path=目标目录/相对路径` 逐个分片上传，服务端自动建父目录；传输面板显示**整体进度**（已完成数/总数）。

## 8. 关键注意点

1. **分片上传**：photos 与 drive 共用 `web/src/shared/util.js` 的 `chunkedUpload()`；断点记录 IndexedDB 列为后续项
2. **下载断点**：`Range` + `ReadableStream` 已支持 206；IndexedDB / File System Access API 中转断点续传列为后续项
3. **401 全局**：`shared/client.js` 统一拦截 → 清 token → `/portal/pair.html`（已实现）
4. **缩略图懒加载**：相册/云盘用 `loading="lazy"`（已实现）；`IntersectionObserver` 列为后续优化
5. **server_url 配置（可选）**：首版同源省略；若将来前端独立部署，在 client.js 增加 baseUrl 配置
6. **i18n**：文案暂为硬编码中文；vue-i18n（zh-CN/en-US）列为后续项
7. **存储位置可配置**：相册/笔记落盘路径经环境变量或 `.env` 配置（见 `.env.example`）

## 9. 完成定义

- [x] portal：欢迎区 + 宫格（`/api/portal/apps` 配置驱动）+ 配对页 + 401 拦截
- [x] album：网格浏览 + 预览 + 分片上传 + Range 下载 + 删除 + 分页 + 磁盘余量
- [x] drive：盘切换 + 面包屑 + 文件操作全套 + 回收站 + 传输面板 + 批量操作
- [x] 三模块同源共享登录态；聚合页在 index，笔记仅经聚合页进入（UA 分发保留）
- [x] 上传分片、下载 Range 206、匿名缩略图/下载（可选鉴权）验证通过
- [x] 移动端/窄屏响应式可用（CSS 网格自适应）
- [ ] 断点续传（IndexedDB 记录已传分片 / File System Access API）
- [ ] **下载文件夹（zip）** + **上传文件夹（webkitdirectory + 相对路径）**
- [ ] i18n（vue-i18n zh-CN/en-US）
- [ ] 前端独立部署时的 server_url 配置项
