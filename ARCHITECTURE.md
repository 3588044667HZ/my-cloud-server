# 云备份服务架构设计

> 状态：设计稿 v1
> 定位：为"相册 + 笔记"提供自建云备份服务；**服务端落地于新项目
> `E:\PyCharmProjects\my-cloud-server`**（mock-server 冻结为 legacy，仅作 notes 逻辑迁入源）。
> 客户端为独立 Android APK（未来 PC 复用同一套网络 API）。
> 关联：服务端骨架见 `my-cloud-server/SERVER_SKELETON.md`；
> 本仓库另含 `UNIFIED_DEVICE_TOKEN_AUTH_SPEC.md`（鉴权规范）。

## 一、系统组成与拓扑

```
┌───────────── 客户端（同一套 REST API，多端复用） ─────────────┐
│                                                              │
│  Android 云服务 APK           PC 客户端(后续)                  │
│  ┌─────────────┐              ┌─────────────┐                │
│  │ gallery-agent│(采集MediaStore)│ core 复用   │(仅换采集/UI)   │
│  └──────┬──────┘              └──────┬──────┘                │
│         └─────── 共用 core 层 ───────┘                        │
│  core: protocol / syncengine / transport / scheduler / storage│
└───────────────────────────────┬───────────────────────────────┘
                                │ HTTPS over Tailscale (100.x)
┌───────────────────────────────▼───────────────────────────────┐
│ 服务端 my-cloud-server（瘦客户机 all-in-one, 4C8G, 3T+500G×2） │
│  Flask + Waitress（单进程）                                     │
│  core: auth(设备token) / plugin_registry / file_store / 队列    │
│  plugins: notes(迁入) · photos · drive(云盘)                    │
│  数据: SQLite 本地盘 + 相册/SMB 文件走本地路径, 经 SMB 共享       │
└───────────────────────────────────────────────────────────────┘
```

**关键事实与约束**

- **All-in-one**：瘦客户机 = SMB 服务器 = 应用服务器 = 存储。SMB 只是把这同一份
  数据共享给外部设备（PC/手机）浏览备份，**应用自身一律用本地路径访问**，不走 SMB 协议。
- 因此：SQLite 用本地路径，无 SMB 文件锁问题；不要在瘦客户机上把自身 SMB 共享
  挂载回本地给应用使用（loopback SMB）。
- 存储：3TB 机械盘（WD 紫盘，7x24 监控盘），容量对相册足够 → **原图 + 压缩图都存**。
- 安全通道：Tailscale 内网（WireGuard 加密），服务只监听 Tailscale 网卡，无需公网证书。
- PC 端不单独建后端，只是另一个客户端，**网络 API 契约是唯一跨端契约**。

## 二、核心设计原则

1. **单用户多设备**：系统只有 1 个用户、任意多设备。账号体系退化为**设备配对**
   （配对码 + 设备 token），不做用户注册/多租户。服务器是唯一事实源，
   每设备独立 `lastSyncTime`；相册去重靠 md5 内容寻址，多设备传同一文件只存一份。
2. **元数据与文件分离**：各插件 SQLite 管元数据（`{code,msg,data}` + 增量 anchor），
   文件内容寻址（`fileId = md5(内容)`）统一由 `core/file_store` 管理，跨插件去重。
3. **增量同步**：每设备维护游标（`since` 时间戳 / anchor），只传变更；删除用 tombstone。
4. **插件化**：每领域独立成插件（独立库/迁移/路由/任务处理器），
   通过注册表启停；新增插件 = 注册，不复制代码。
   **所有服务平权**：现有 notes 与未来服务一律是插件——新服务走 GreenfieldPlugin，
   既有实现走 AdapterPlugin 兼容壳（框架层平权）再内部演进（见 §5.1）。
5. **核心平台共享**：auth（设备级）、protocol、任务队列、文件层、事件总线属 core，
   不随插件拆分。
6. **平台与 core 解耦**：客户端 `core` 层无 Android 依赖，PC 复用编译产物。
7. **权限闸门默认关闭**：任何"读整机磁盘"类能力（如云盘文件管理）默认关闭、显式开启。

## 三、客户端架构（Android，PC 复用 core）

```
com.example.cloudbackup/
├── core/                    # 纯 Kotlin/Java，无 Android 依赖
│   ├── protocol/            # REST 客户端：请求/响应、重试、鉴权头、401 自动 refresh
│   │   └── PhotoApi.kt / DriveApi.kt   # ← 与 PC 共享的 API 契约实现
│   ├── syncengine/          # 泛化同步引擎（见 §7.2）
│   │   ├── SyncEngine.kt    # 通用 backup/recovery 流程
│   │   └── modules/         # 各模块只有 schema + 采集器
│   ├── transport/           # 分片上传、断点续传、MD5 幂等
│   ├── scheduler/           # 同步触发策略（纯逻辑，平台注入触发源）
│   ├── storage/             # change_log / anchors / device_map（接口抽象）
│   └── model/               # SyncItem, ChangeLog, SyncResult
├── android/                 # Android 平台适配
│   ├── service/             # CloudSyncService（前台服务）+ JobScheduler
│   ├── gallery/             # MediaStore 采集、FileObserver、快照比对删除检测
│   └── ui/                  # 开关、存储占用、同步状态、云盘文件管理
└── pc/                      # (后续) 复用 core，UI 用 Web/Electron
```

**解耦原则**：`core` 不 import 任何 Android 类；`storage` 为接口
（Android 用 Room，PC 用 SQLite/文件）；下载/预览组件在平台层复用。

### 3.1 UI/UX 基线（Android）

> 目标：**保证"不复古"**。OPPO Cloud 用 View + 自研 NX 令牌系统是因为要兼容老机型；
> 本项目为全新 App，无历史包袱，直接上现代方案。

**技术栈（硬保证）**：Jetpack Compose + Material 3。自带动态取色、tone 层级、
圆角、排版、明暗主题，开箱即现代。禁止用 XML+View 搭新界面。

**设计令牌**：颜色一律走 `Theme.kt`，不写死：

```kotlin
// Theme.kt
val colorScheme = dynamicLightColorScheme(context)      // Android 12+ 动态取色
    ?: lightColorScheme(primary = /* 种子色 */)
val darkScheme  = dynamicDarkColorScheme(context)
// + isSystemInDarkTheme() 跟随系统
```

**导航骨架**：底部导航（NavigationBar）3~4 个 Tab：**首页 / 相册 / 云盘 / 设置**。
禁止用设置列表当主页（复古感最大来源）。

**首页 = 卡片仪表盘**（对标 OPPO `cloud_home_item_{space,sync,tool}`）：
存储用量环形卡 + 相册/笔记/云盘模块卡 + 同步状态卡 + 入口 Banner。
功能平铺成设置页 = 淘汰方案。

**云盘文件管理器交互规范（复古重灾区，重点防）**：
- 面包屑导航 + 列表/网格切换 + 缩略图懒加载 + 长按多选
- 操作走**底部面板（BottomSheet）**；下载进度用 Snackbar/进度条
- 参考 Google Files / Solid Explorer 的交互，用 Material 3 组件实现
- **禁止**：顶部路径输入框 + 纯列表 + 返回键层层退（ES 文件管理器那种 2010 交互）

**其它**：系统字体；正确密度/间距/圆角（Material 3 默认值）；少用自定义花哨控件。

### 3.2 界面级蓝图

#### 导航骨架

底部导航 4 Tab：**首页 / 相册 / 云盘 / 设置**（`NavigationBar` + Navigation 组件）。
首次启动进入**设备配对引导**（输入配对码 → 绑定 → 进入首页）；未配对时首页显示配对引导卡。

#### Tab 1 · 首页（卡片仪表盘）

```
┌────────────────────────────────┐
│ 云备份            [设备名▾][⚙]  │  顶栏：标题 + 当前设备 + 设置快捷
│ 上次同步 2 分钟前        [⟳]     │
├────────────────────────────────┤
│ ┌────────────────────────────┐ │
│ │ 存储空间    ╭─────╮          │ │  存储用量环形卡
│ │             │ 62% │  1.2T/3T │ │  明细行：相册 800G · 笔记 120M · 云盘 400G
│ │             ╰─────╯          │ │  点击 → 存储明细页
│ └────────────────────────────┘ │
├────────────────────────────────┤
│ ┌─相册备份────────┐ ┌─笔记────┐ │  模块卡（图标+名称+状态摘要+跳转）
│ │ ● 已备份 8,432  │ │ 12 条   │ │
│ │ 待传 5 · 失败 2 │ │ 已同步   │ │  相册卡右下角小字状态色点
│ └─────────────────┘ └─────────┘ │
│ ┌─云盘────────────────────────┐ │  云盘卡：各盘空间条
│ │ 3T 62%▮▮▮▮▮▮▯  500G▮▯ 500G▮▯│ │
│ └────────────────────────────┘ │
├────────────────────────────────┤
│ 传输队列                         │  进行中任务（可暂停/取消/重试）
│ ↑ photo_2026.jpg   78%  ⏸ ╳     │
│ ↓ 课程/数据结构.pdf  12%  ⏸ ╳    │
├────────────────────────────────┤
│ 最近动态                         │  最近备份照片/文件横向滚动缩略图
│ [图][图][图][图][图]  [全部→]     │
└────────────────────────────────┘
```

#### Tab 2 · 相册

```
┌────────────────────────────────┐
│ ‹ 相册备份           [搜索][⚙]  │  顶栏
│ [全部|已备份|待备份|失败]         │  FilterChip 状态筛选
├────────────────────────────────┤
│ 2026 年 8 月                    │  按拍摄日期分组
│ [图][图][图][图][图][图]        │  网格（3 列），缩略图角标：
│ [图][图][图][图][图][图]        │   ✓ 已备份 / ◎ 待上传 / ! 失败
│ 2026 年 7 月                    │
│ [图][图][图][图][图][图]        │
├────────────────────────────────┤
│ ⬆ 待上传 5 · 失败 2      [重试]  │  底部同步状态条（常驻）
└────────────────────────────────┘
```

交互：
- 点照片 → 全屏预览（左右滑），底部操作：**下载原图 / 删除云端 / 从本机移除**
- 长按 → 多选 → 底部面板：批量删除云端 / 全部下载
- `⚙`：自动备份开关、仅 Wi-Fi 传原图、存储策略（压缩图/原图+压缩图）、视频阈值
- 空状态：未开启备份 → 引导卡（"开启自动备份" + 说明）

#### Tab 3 · 云盘（文件管理器）

```
┌────────────────────────────────┐
│ ‹ 云盘               [搜索][∷]  │
│ [3T 盘 ▾] / 课程 / 数据结构     │  盘选择下拉 + 面包屑（可点回任意上级）
│ [网格][列表]  [按时间▾]         │  视图切换 + 排序
├────────────────────────────────┤
│ 📁 2026年课程            1.2G   │  文件夹/文件混排
│ 📁 毕业论文              340M   │  图片显示缩略图
│ 🖼 结构图_v2.webp         2.1M  │  视频显示帧缩略图
│ 📄 论文终稿.docx          480K  │
│ 🎬 演示录制.mp4           1.5G  │
│ 📁 照片备份              800G   │  备份目录（默认隐藏，开关显示）
├────────────────────────────────┤
│              [+ 新建]  (FAB)    │  新建文件夹 / 上传文件 / 拍照上传
└────────────────────────────────┘
```

交互：
- 点文件夹 → 进入（面包屑推进）；点面包屑 → 回上级/回盘根
- 点文件 → 图片/视频内嵌预览，其他文件下载
- 长按多选 → **BottomSheet 操作面板**：下载 / 移动 / 复制 / 重命名 / 删除 / 分享
- 删除 → 进回收站（Snackbar 带"撤销"）；回收站入口在 `∷` 菜单
- 跨盘操作：盘选择下拉切换目标盘
- 搜索：跨全部盘按文件名

#### Tab 4 · 设置

```
┌────────────────────────────────┐
│ 设置                             │
├────────────────────────────────┤
│ 设备                             │
│  本机 (OPPO Reno)      ● 当前    │  设备名/最后同步时间/撤销
│  PC-1 · 2 天前同步        [×]    │
├────────────────────────────────┤
│ 相册备份                         │
│  [开关] 自动备份             ON  │
│  仅 Wi-Fi 上传原图          ON  │
│  存储策略            ▸ 压缩图    │
│  视频上传阈值          ▸ 100 MB  │
├────────────────────────────────┤
│ 云盘                             │
│  [开关] 云盘功能             ON  │
│  显示备份目录                OFF │
│  回收站              ▸ 2.3 GB    │  进入回收站管理
├────────────────────────────────┤
│ 通知 · 关于                       │
│  备份完成通知                 ON  │
│  服务器地址       100.82.x.x     │
│  版本 v0.1.0                     │
└────────────────────────────────┘
```

#### 关键状态与通用组件

- **加载/空/错误三态**：列表、相册、云盘统一骨架屏 + 空状态插图 + 错误重试
- **同步状态语义**：`已最新(绿) / 同步中(进度) / 待上传N(黄) / 失败(红可重试)`，全局同款色点组件
- **预览组件复用**：图片/视频查看器在相册、云盘、首页"最近动态"三处共用
- **配对引导**：配对码大号输入框 + 扫码（可选）+ 连接状态动画
- 所有操作反馈用 Material 3 标准组件（Snackbar/对话框/BottomSheet），不做自定义弹窗

## 四、网络 API 契约（跨端唯一契约）

统一 `{code, msg, data}`，`code:0` 成功；分页字段沿用 notes 的
`pagination`。token 过期 401 → 客户端自动 refresh → 重试。

### 4.1 photos（相册备份）

```
POST /api/photos/register              # 两阶段注册：返回 id + 压缩图/原图 URL（幂等）
  req: { clientId(UUID), mediaId, filename, width, height, size, md5 }
PUT  /api/photos/{id}/file             # 补传文件（分片/整包），服务端生成缩略图
  Header: Content-MD5 可选
GET  /api/photos/sync?since=ISO8601    # 增量：{updated:[Photo[]], deletedIds:[], serverTime}
GET  /api/photos/{id}/download?size=thumb|orig   # 压缩图/原图，永不 404（占位图兜底）
PUT  /api/photos/{id}                  # 元数据写回（md5/fileId 同步后）
DELETE /api/photos/{id}                # tombstone（软删）
```

分片上传（大视频）：
```
POST /api/photos/upload/init                      # 申请 uploadId
PUT  /api/photos/upload/{uploadId}/{seq}          # 4MB/片
POST /api/photos/upload/{uploadId}/complete       # 合并 + md5 校验

GET  /api/space                                   # 相册存储剩余空间（磁盘满保护，见 §5.4）
```

`since` 语义、`If-Match` 乐观锁、tombstone 均照抄 notes 模块，两端认知一致。

### 4.2 drive（云盘文件管理，见 §6）

### 4.3 鉴权（单一凭证：设备 token）

**全部服务（notes/photos/drive）共用一个 token**：设备 token（opaque、长期有效、
可撤销），无 JWT/refresh 链。实现细节见 `UNIFIED_DEVICE_TOKEN_AUTH_SPEC.md`
（服务端）与各端仓库内 `AUTH_DEVICE_TOKEN_REFACTOR.md`（三端改造）。

```
1. 首台设备 → 管理后台/终端生成配对码（AB3K-D9QX，600s 一次性）
2. 各端     → POST /api/devices/pair         # 配对码换取设备 token
3. 所有请求带 Authorization: Bearer <device_token>
4. core.auth.require_device → 设备 + 单用户（devices 表，token 只存哈希）
5. 管理后台 /api/admin/devices              # 设备列表/撤销（撤销即失效）
```

- token 绑定 `deviceId`；每设备独立 `lastSyncTime`（与 notes 客户端一致）
- **分支约定**：`main` 保持现状，`dev-统一云服务` 分支一次性切换（无迁移态）：
  服务端删 `/api/auth/login|register|refresh`，三端改配对码登录、去 refresh、删明文密码
- 安全：迁移即删除明文密码存储；token 只存加密存储（Android DataStore / localStorage 仅存 token）

## 五、服务端设计（my-cloud-server）

### 5.1 插件化架构（每领域独立成插件）

**决策：独立建库 + 进程内插件。** 每个领域（notes/photos/drive…）是一个自包含插件：
独立 SQLite 库、独立 schema 迁移、独立路由命名空间、独立后台任务处理器，
通过插件注册表启停。账号/鉴权作为**核心平台**由所有插件共享，不随插件拆分。

进程内插件（而非微服务/独立进程）的理由：
- 瘦客户机 4C8G，单进程足够；微服务多进程多端口多部署是负担
- 插件接口按"可提取"设计——未来某插件变重（OCR/视频转码）时，
  仅需把该插件的任务处理器移到独立 worker 进程，接口不变

```
server/
├── core/                      # 核心平台（所有插件共享）
│   ├── auth/                  # 设备配对、设备 token、权限中间件、设备管理
│   ├── plugin_registry.py     # 插件扫描/注册/启停（config 指定启用列表）
│   ├── db.py                  # 插件数据库生命周期（连接/迁移/关闭）
│   ├── file_store/            # 统一文件层：内容寻址 md5 + 全局引用计数 + 统一 GC
│   │                          #   → 跨插件文件去重、生命周期唯一权威
│   ├── event_bus/             # 进程内事件总线（发布/订阅，轻量）
│   ├── task_queue.py          # 共享后台任务队列（SQLite + 单 worker）
│   └── protocol.py            # {code,msg,data}、分页、错误码
└── plugins/
    ├── notes/                 # 笔记插件（既有实现：AdapterPlugin 兼容壳，见下）
    │   ├── plugin.py          # 插件清单（stage=adapter）
    │   └── notes.db
    ├── photos/                # 相册插件（Greenfield）
    │   ├── plugin.py
    │   └── photos.db
    └── drive/                  # 云盘文件管理插件（Greenfield）
        ├── plugin.py
        └── smb.db
```

```python
# plugins/xxx/plugin.py —— 插件契约
class Plugin:
    name: str
    version: str
    db_file: str | None        # 独立库；None = 纯文件型插件
    migrations: str            # 插件独立 alembic 版本
    routes: Blueprint          # 独立路由命名空间
    task_handlers: dict[str, Callable]
    def on_enable(self, core): ...
    def on_disable(self, core): ...
```

**全服务平权（既有 notes 与未来服务一律是插件）**：

新服务 = `GreenfieldPlugin`（直接建在框架上）；既有实现 = `AdapterPlugin` 兼容壳，
**框架层先平权，内部渐进重构**：

```python
class NotesAdapterPlugin(Plugin):
    """既有 notes 的兼容壳：注册表只认 Plugin 接口，实现新旧一律平等。"""
    name = 'notes'
    stage = 'adapter'            # 演进: legacy → adapter → converged
    db_file = 'data/notes.db'    # 沿用现有库，路由/表不动
    routes = notes_bp            # 挂载既有 blueprint

    def on_enable(self, core):
        @self.routes.before_request
        def _map_identity():
            # 单一凭证（见 §4.3）：require_device 已注入 g.user_id，壳层直接透传
            pass
```

**notes 客户端同步归客户端（云服务不介入）**：笔记原生 App（Kotlin/Compose +
Retrofit + Room + DataStore）与 Web 端已实现完备双向同步（`since` 增量、If-Match、
RESTORE、用户裁决冲突）。云服务对笔记**零同步职责**，仅作首页状态卡与跳转入口。
云服务 Android 端复用相同分层与存储技术（`data/remote` + `data/local` + repository），
PC 端 = `static/cloud/` Vue 3 Web App 随 Flask 分发。

演进三阶段（各阶段独立可上线，不阻塞其它插件）：

| 阶段 | 状态 | 说明 | 风险 |
|---|---|---|---|
| `legacy` | 现状 | 独立 accounts.db 鉴权 + user_id 维度 | 不动 |
| `adapter` | 目标首版 | 包壳进注册表；鉴权改由 core 设备认证 → 映射单用户；路由/表/业务不动 | 低 |
| `converged` | 远期 | 内部彻底平权：去 user_id、文件走 `file_store`、事件走 `event_bus` | 高，最后做 |

约束与边界：
- **鉴权不插件化**：插件一律用 `core.auth.require_device`（单一设备 token，见 §4.3）；
  drive 的开关表在 `drive.db`，判定基于核心设备身份；
  notes adapter 壳层不再做凭证映射（require_device 已注入 `g.user_id`）
- **跨插件操作无分布式事务**：独立库无法跨文件事务。"从 SMB 保存到相册"= 复制语义，
  读 drive 流式 → 写 photos 独立库 → 幂等重试，不追求一次性原子
- **迁移**：每插件独立 `alembic_version`，升级/回滚互不影响
- **任务**：共享队列 + 插件注册 `task_handlers`，worker 按 kind 路由

### 5.2 插件间协作（跨插件业务联动）

多插件必然相互联系，三类交互模式：

| 模式 | 场景 | 机制 |
|---|---|---|
| **复制** | SMB→相册"保存到云备份"、SMB→笔记"附到笔记" | 插件暴露 service 接口，另一插件经注册表进程内直接调用（读流→写目标库→幂等重试） |
| **引用** | 笔记插图引用相册 `file_id` | 不拷贝文件，插件表存 `file_id` 引用，`core/file_store` 引用计数；删除由各插件管理引用、core 统一 GC |
| **事件** | 照片同步完成→通知其它端、文件变更→drive 目录刷新 | 插件发布 `photo.uploaded` / `file.ref.updated`，订阅方异步处理 |

**原则：元数据各插件独立，文件统一由 `core/file_store` 管**——文件生命周期唯一权威
是 file_store 的引用计数，跨插件去重（同一张图在相册和笔记附件只存一份）。

### 5.3 插件数据模型示例（photos）

```sql
-- plugins/photos/photos.db
CREATE TABLE photos (
  id TEXT PRIMARY KEY,              -- 服务端 id
  device_id TEXT,                   -- 上传来源设备（标记用途，不做多租户）
  client_id TEXT UNIQUE,            -- 客户端 UUID，幂等键
  media_id TEXT,                    -- MediaStore rowId（原设备标记）
  filename TEXT, width INT, height INT, size INT,
  thumb_file_id TEXT, orig_file_id TEXT,
  md5 TEXT, version INT,
  created_at TEXT, updated_at TEXT, deleted_at TEXT
);
CREATE INDEX idx_photos_updated ON photos(updated_at);
```

> 文件引用计数统一由 `core/file_store` 维护（全局，跨插件），不在插件内建 `file_refs`。

### 5.4 文件存储（多盘 + 机械盘适配）

**硬件拓扑**：SMB 从盘根共享，共 3 块盘 —— 3TB（紫盘）+ 500G ×2。

```
瘦客户机本地:
  3T (紫盘, 7x24)  → PHOTOS_DIR: 相册原图 + 压缩图（主存储）
                      + 系统 SQLite（本地路径, 机械盘单用户量级足够）
  500G (盘1)       → 笔记/附件/通用应用数据（C:/open-note-server/data/...）
  500G (盘2)       → 通用文件共享（云盘浏览主体内容）
  每块盘经 SMB 共享（盘根）→ PC 经 Tailscale 访问/拉备份

  相册目录:
  {PHOTOS_DIR}/orig/{ab}/{cd}/{md5}      # 原图
  {PHOTOS_DIR}/thumb/{ab}/{cd}/{md5}.webp # 压缩图
```

- 目录按 md5 前 4 位两级分桶，避免单目录文件过多（机械盘目录遍历慢）
- 文件引用计数统一在 `core/file_store`（全局），软删不物理删；GC 批量回收（减少机械盘碎片）
- 缩略图规格复用现有：最长边 ≤1280px、WEBP q70、目标 ≤200KB
- **磁盘满保护**：上传原图前查询 `PHOTOS_DIR` 剩余空间，低于阈值（默认 10GB）
  → 暂停原图上传、仅接收缩略图，并向客户端/管理后台告警
  （对应客户端 `GET /api/space` 检查，类似 OPPO 的 `TUNNEL_QUERY_SPACE_ENOUGH`）
- 多卷扩展（3T 装满后加第二存储卷）列为 P2，首版单一 `PHOTOS_DIR` 配置项即可

### 5.5 后台任务队列（必须尽早引入）

缩略图生成、分片合并、GC、未来的视频转码**不在请求路径里同步做**：

```
SQLite 表作队列 + 单 worker 线程：
  tasks(id, kind, payload_json, status, attempt, created_at)
worker 循环: 取 PENDING → 执行 → 置 DONE/FAILED(重试, 指数退避)
```

### 5.6 schema 迁移

从第一个新插件起引入 **alembic**（SQLite 支持良好）。每插件独立版本表
（`alembic_version` 在各插件自己的库内），升级/回滚互不影响。
模块多了表结构演进是必然，迁移工具越晚引入越痛苦。

## 六、云盘（SMB 文件管理）入口

> **定位**：云盘不是"只读浏览"，而是**完整文件管理器**——对标 ES 文件管理器的
> SMB 界面，是核心功能之一。管理瘦客户机全部磁盘上的已有课程/文件等。

### 6.1 原则

- **客户端只走 HTTP**：云盘界面的上传/下载一律经 `/api/drive/*`（HTTP Range 流式 +
  分片上传），**客户端不直接用 SMB 协议**。SMB 仅保留给 PC 资源管理器浏览与整盘拉备份。
- **直读本地磁盘**：服务端 share → 本地路径由配置声明，后端用本地路径读写，不经 SMB 协议
  （无锁、无协议开销）
- **配置即白名单**：对外共享哪些盘由 `config.py: DRIVE_SHARES` 声明（`enabled=False` 的盘
  如系统盘不进云盘）；管理后台仅保留全局总开关（关闭 = 整个云盘功能禁用）
- **备份目录默认隐藏**：`HIDDEN_BACKUP_DIRS`（如 `D:/云备份`）在云盘默认不显示
  （md5 分桶对用户无意义且易误删），提供"显示备份目录"开关兜底
- **独立模块**：与相册/笔记平级，通过插件架构注册

### 6.2 目录规划（配置声明）

```python
# config.py
DRIVE_SHARES = [
    {"share": "DATA3T",  "local_path": "D:\\", "label": "3T 数据盘", "enabled": True},
    {"share": "DATA500A","local_path": "E:\\", "label": "500G-1",    "enabled": True},
    {"share": "DATA500B","local_path": "F:\\", "label": "500G-2",    "enabled": True},
    {"share": "SYSTEM",  "local_path": "C:\\", "label": "系统盘",     "enabled": False},
]
HIDDEN_BACKUP_DIRS = ["D:\\云备份"]
PHOTOS_DIR = "D:\\云备份\\photos"
```

```
D:\ 云备份/ ← 备份数据目录（云盘默认隐藏，HIDDEN_BACKUP_DIRS）
   photos/   ← PHOTOS_DIR
   notes/    ← 笔记库 + 附件
D:\ 其余、E:\、F:\ ← 已有课程/文件，云盘可见（DRIVE_SHARES 中 enabled 的盘）
C:\         ← 系统盘，enabled=False，云盘不可见
```

### 6.3 ShareResolver 抽象（配置驱动 + 存储可迁移）

> 共享清单由 `config.py: DRIVE_SHARES` 声明（share → 本地路径，`enabled` 过滤），
> **不再枚举注册表**——配置即白名单，即沙箱根。
> `list_shares` 返回 enabled 的盘；`resolve(share)` 返回配置的本地根路径。

```python
class ShareResolver:                      # 接口
    def list_shares(self) -> list[Share]  # 名称/本地路径/标签/剩余空间
    def resolve(self, share: str) -> str  # share名 → 本地根路径（配置给出）

class ConfigShareResolver(ShareResolver): # 当前实现：读 config.DRIVE_SHARES
    # 过滤 enabled=False；系统盘在配置里显式 disabled，零歧义

class RemoteSmbResolver(ShareResolver):   # 未来：SMB 迁走后，用配置的 share 名走 smbprotocol
    pass
```

### 6.4 权限模型（配置白名单 + 全局总开关）

```
- 部署声明：config.py: DRIVE_SHARES（enabled 即白名单）——没写就不暴露
- 运行时开关：drive_settings.enabled（管理后台总开关，关闭 = 整个云盘禁用）
```

```sql
-- plugins/drive/drive.db
CREATE TABLE drive_settings (
  key TEXT PRIMARY KEY, value TEXT      -- 'enabled' = 0/1 全局总开关
);
```

- 默认关闭（配对该功能后再开）；管理后台开关
- 每次请求 `401 未授权设备 → 403 云盘未开启`
- 配置与开关分离：配置管"有哪些盘"，开关管"云盘是否可用"

### 6.5 API 入口（命名空间 `/api/drive`，完整文件操作）

```
GET    /api/drive/shares                  # 盘列表（含容量余量，见下）
GET    /api/drive/list?share=X&path=/a/b  # 列目录
       resp: { entries: [{name,isDir,size,mtime,ext}], pagination }
GET    /api/drive/stat?share=X&path=/a/b/f.txt   # 单文件元信息
POST   /api/drive/mkdir?share=X&path=/a/new
POST   /api/drive/rename?share=X&from=/a&to=/b
POST   /api/drive/move?share=X&src=/a&dst=/b     # 同盘原子；跨盘=复制+删除
POST   /api/drive/copy?share=X&src=/a&dst=/b
DELETE /api/drive/delete?share=X&path=/a/b       # 移入回收站（见 §6.6）
GET    /api/drive/download?share=X&path=/a/b/v.mp4    # 下载/预览（流式 + Range）
GET    /api/drive/download?share=X&path=/folder&zip=1 # 下载整个文件夹（流式 ZIP，见下）
POST   /api/drive/upload                           # 分片写盘 + 断点续传（path 含未存在子目录时自动建父目录）
GET    /api/drive/thumb?share=X&path=/a/b/p.jpg    # 图片预览（Pillow, 磁盘缓存）
GET    /api/drive/search?share=X&q=keyword&dir=/a  # 文件名搜索
GET    /api/drive/sign?share=X&path=/a/b/v.mkv&expires=600   # 签名临时 URL（外部播放器用）
```

**每盘余量**（`GET /api/drive/shares` 响应，服务端用 `shutil.disk_usage(盘根)`）：

```json
{ "code": 0,
  "data": [ { "share": "DATA3T", "label": "3T 数据盘",
              "total": 3000592982016, "used": 1860000000000, "free": 1140592982016,
              "freePercent": 38 } ] }
```

`path` 一律相对 share 根，`/` 分隔，禁止绝对路径与 `..`。
`upload` 复用相册分片上传的同一套 `transport` 组件。

**签名临时 URL**（`GET /api/drive/sign`，供外部播放器/浏览器地址栏等无法带鉴权头的场景）：

```
GET /api/drive/sign?share=X&path=/a/b/v.mkv&expires=600
  resp: { code:0, data: { url: "/api/drive/download?share=X&path=/a/b/v.mkv&exp=1688888888&sig=<hmac>" } }
```

- `sig` = HMAC-SHA256(签名密钥, `share|path|exp`)，短时有效（默认 600s，可配置）
- download 收到签名参数时：校验 `exp` 未过期 + `sig` 匹配 → 免 `Authorization` 头放行；
  签名缺失/失效 → 仍要求设备 token
- 用途：Android 端"用 VLC 播放"（`ACTION_VIEW` 打开签名 URL）、浏览器直开视频等
- 签名密钥服务端配置（`config.py: SIGN_SECRET`），泄露需轮换

**下载整个文件夹**（主方案：**逐文件目录下载**，OPPO 云盘模式）：

- 客户端 `GET /api/drive/list`（递归列目录）→ 逐个文件 `GET /api/drive/download`（Range 流式）
  → 本地按相对路径重建目录结构
- **服务端零新增**（复用现有 list + download）；逐文件 Range 断点、可暂停/续传、总进度
- 可选"打包下载"：`GET /api/drive/download?share=X&path=/folder&zip=1` → 流式 ZIP
  （适合分享/导出；服务端需压缩 CPU/临时空间，大文件夹慎用）

**上传整个文件夹**（`POST /api/drive/upload`）：

- `path` 参数允许带未存在的子目录（如 `path=/课程/2026/结构图.pdf`），
  服务端**自动创建父目录**后落盘
- 客户端遍历文件夹，逐文件带**相对路径**上传（见 Web/Android 端 spec），无需先逐个 mkdir

### 6.6 删除保护与回收站

```
DELETE /api/drive/delete  → 移到 {盘根}/回收站/<原相对路径>
POST   /api/drive/trash/list           # 查看回收站
POST   /api/drive/trash/restore        # 还原
DELETE /api/drive/trash/empty          # 清空
```

- 删除一律进回收站（防手滑删课程/文件）；清空回收站才物理删除
- 跨盘 move 失败时回退（源文件保持，目标已复制部分清理）

### 6.7 路径沙箱（安全核心）

```python
def safe_path(share: str, path: str) -> str:
    root = resolver.resolve(share)
    real_root = os.path.realpath(root)
    real = os.path.realpath(os.path.join(real_root, path.lstrip('/')))
    if real != real_root and not real.startswith(real_root + os.sep):
        raise Forbidden()            # 越界（含 junction/symlink）→ 403
    if not os.path.exists(real):
        raise NotFound()
    return real
```

要点：`realpath` 解析符号链接/junction（Windows 上 junction 是常见越界手段）；
拒绝 `..`、空 share、未授权 share；`list_shares` 时过滤 `$` 隐藏共享与系统盘。

### 6.8 传输

- 下载用 Flask `send_file(conditional=True)`：原生支持 Range（HTTP 206），
  视频拖动进度条/大文件断点续传直接可用
- 后端直读本地机械盘顺序流式发送，不整读进内存
- 缩略图：Pillow 生成 + 磁盘缓存（与相册共用工具函数）
- 上传：分片 4MB + 断点续传，写盘后校验 md5

### 6.9 客户端入口

```
App 导航 → "云盘"（文件管理器）
├── core/drive/                # DriveApi + 目录状态机/缓存/分页/多选操作（PC 复用）
└── android/ui/                # 列表/网格视图 → 长按多选 → 操作菜单 → 预览/下载
```

- 交互对标 ES 文件管理器：双栏/网格、多选、批量移动复制删除
- "下载到本地"/"保存到相册" = 走 photos 的 `register + upload`，打通插件
- 备份目录（`云备份/`）默认隐藏，"显示备份目录"开关在设置里

## 七、同步引擎与扩展性

### 7.1 同步流程

```
── 备份（Android）─────────────────────────────────────────
1. gallery-agent 采集到新照片 → SyncItem → 写本地 change_log(dirty)
2. scheduler 放行（缩略图：任意网络即传；原图：Wi-Fi+充电）
3. transport: md5 → register → 分片 PUT → complete
4. POST /api/photos/sync 上传变更 → 返回新 serverTime → 标记 clean

── 恢复（PC / Android 换机）─────────────────────────────
1. GET /api/photos/sync?since=last → updated[] + deletedIds[]
2. 原图懒加载（download?size=orig）；预览只拉 thumb
3. 合并本地 → 更新 last serverTime

── 删除检测（仅 Android 采集端）──────────────────────────
本地 photo_snapshot 表 + Wi-Fi 时全量 MediaStore 比对 → tombstone → 随 sync 上传
```

### 7.2 泛化同步引擎（扩展性关键）

客户端 `core/syncengine` 按模块注册表泛化，**不写特化 agent**：

```
core/syncengine/
  SyncEngine(moduleDef, serializer, transport)   # 通用 backup/recovery
  modules/photos.kt                               # 只有 schema + 采集器
  modules/xxx.kt                                  # 新增 = 注册, 不复制引擎
```

### 7.3 关键机制

| 机制 | 实现 |
|---|---|
| 幂等/去重 | 客户端 `clientId=UUID` 幂等键；`fileId=md5` 内容寻址，同图只存一份 |
| 断点续传 | 分片以 `uploadId` 记录已传分片，重传仅补缺失片 |
| 冲突 | `If-Match: updatedAt`（照抄 notes）+ 服务器时间戳，last-write-wins 兜底 |
| 调度约束 | 原图仅 Wi-Fi+充电；省电模式暂停；失败指数退避重试 |
| 删除 GC | 引用计数 + 后台批量回收 |
| 永不 404 | download 未就绪返回占位图 + `X-Photo-State: pending/ready`（照抄附件 spec） |
| 推送预留口 | `syncengine` 定义 `PushListener` 接口；先实现轻量轮询，将来换 WebSocket/MQTT 不推翻架构 |

### 7.4 明确不扩展的边界

| 场景 | 现状 | 何时要换 |
|---|---|---|
| 多用户大规模 | 单 Flask + SQLite | 用户 > 几十或并发写高 → Postgres + 多 worker |
| 重处理 | 瘦客户机 4C8G | 视频转码/OCR → 独立 worker 或另一台机 |
| 实时协同编辑 | 无 | 需要时再加推送通道（口已留） |

## 八、存储与容灾（all-in-one 约束）

```
瘦客户机本地:
  SQLite    C:/open-note-server/data/           (本地路径, 无 SMB 锁问题)
  相册文件  {PHOTOS_DIR}  3T 盘                     (本地路径, 盘根 SMB 共享)
容灾（出盒, 后续 PC 做）:
  PC 经 SMB+Tailscale 逐盘拉取: 3T 盘(相册) + 500G(数据) → PC 本地
  SQLite 备份必须先生成 stage（sqlite3 .backup，WAL 下不能直接拷贝 db 文件）
```

## 九、实施里程碑

> 服务端全部在 `E:\PyCharmProjects\my-cloud-server` 落地，顺序见其
> `SERVER_SKELETON.md` §7；此处为整体链路。

1. **服务端基建**：core 平台（设备 token auth / protocol / plugin_registry / file_store
   / event_bus / 任务队列）+ alembic
2. **notes 插件迁入**：从 mock-server 迁入 my-cloud-server（功能等价，仅鉴权变设备 token）
3. **photos 插件**：分片上传/缩略图/增量 sync/下载（curl 可验证）
4. **drive（云盘）插件**：ShareResolver → 文件操作全套(含回收站) → Range 传输
5. **管理后台**：设备管理 + 配对码 + 孤儿清理
6. **数据迁移脚本** + 部署（TAE / 脚本托管）
7. **Android core**：SyncEngine + PhotoApi + DriveApi + transport（联调 my-cloud-server）
8. **Android gallery-agent**：采集 + 删除检测 + 调度
9. **Android UI**：Compose + Material 3 基线（令牌/暗色/底部导航）+ 首页仪表盘
10. **PC 复用**：`static/cloud/` Vue 3 Web App 复用同一 API，零服务端改动

## 十、待定问题

- [x] ~~SQLite 单文件 vs 每模块一库~~ → **已定：每插件独立建库**
- [x] ~~smb 授权粒度~~ → **已定：全局开关（单用户默认全部盘，仅隐藏系统盘）**
- [x] ~~现有 `notes` 是否迁入插件框架~~ → **已定：平权，notes 走 AdapterPlugin
      兼容壳（先 framework 平权，内部 converged 渐进）**
- [x] ~~notes 鉴权如何统一~~ → **已定：全部服务单一设备 token（无 JWT/refresh 链），
      `dev-统一云服务` 分支一次性切换，三端按各仓库 `AUTH_DEVICE_TOKEN_REFACTOR.md` 改造**
- [ ] 分片大小定 4MB 是否合适（Tailscale 直连 vs DERP 中继场景）
- [ ] 设备配对的撤销/重装策略：设备重装后旧 `clientId` 数据归属是否要保留
- [ ] 跨插件"引用"场景（笔记引用相册 file_id）是否首版就要，还是复制语义先跑通
- [ ] notes `converged` 阶段（去 user_id / 文件走 file_store / 双凭证归一）是否首版规划，还是延后
