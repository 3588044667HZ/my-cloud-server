# 云端回收站 + 云端优先恢复（设计）

> 状态：设计定稿，待 agent 实现
> 依据：OPPO 云服务探查结论（Q1 数据保护 / Q5 云端恢复）
> 哲学：**只有备份到云端的数据才是高价值，本地可丢、云端不可丢**
> 关联：`ARCHITECTURE.md` §6.6（drive 回收站）、`SERVER_SKELETON.md`、`SPEC-WEB-FRONTEND.md`、`SPEC-06-DRIVE-MODULE.md`

## 一、云端回收站统一

### 1.1 统一模型（覆盖 drive / photos / notes）

```
软删除 ──► 回收站（保留 30 天，remainDays 倒计时）──► 超期自动清除
            │ restore（单个/批量）
            └ 彻底删除（强警告后物理删除）
```

- 软删：写 `deleted_at`；保留期 **30 天**（可配置 `TRASH_RETAIN_DAYS`）
- `remain_days = TRASH_RETAIN_DAYS - (now - deleted_at)/86400`，客户端离线可展示倒计时
- **超期自动清除**：服务端后台任务（复用 task_queue），每天扫描一次，物理删除超期项

### 1.2 分级确认（防"本地失误连累云端"）

| 动作 | 交互 |
|---|---|
| 普通删除 | 提示"将移入回收站，保留 30 天" → 软删（不强警告） |
| 回收站内彻底删除 | **强警告**"文件将被彻底删除，删除后不可恢复" |
| 清空回收站 | 强警告 + 二次确认 |

### 1.3 统一回收站 API（插件工厂生成，三模块同构）

```
GET    /api/{module}/trash?page=&size=        # 列表：含 deletedAt / remainDays / size
POST   /api/{module}/trash/restore            # 恢复（单 id 或 id[] 批量）
DELETE /api/{module}/trash/{id}               # 彻底删除（强警告后调用）
DELETE /api/{module}/trash/empty              # 清空
```

- drive：现有 §6.6 对齐为本形状（`/api/drive/trash/list|restore|empty` → 统一为上述）
- photos：**新增**回收站（当前只有 tombstone，无 UI/恢复）
- notes：已有 trash/recover/permanent，对齐 remainDays 展示
- 模块工厂按 `ModuleDef` 生成这套路由，三端共用

### 1.4 客户端

- **回收站 UI**（Web/Android）：列表 + 剩余天数 + 恢复/彻底删除/清空
- **离线倒计时**：缓存 `deletedAt + remainDays`，未联网也显示"剩余 N 天"
- **删除分级确认**：普通删提示进回收站；彻底删强警告

## 二、云端优先恢复

> 云端 = 单一事实源。换机/重装 = 配对后从云端恢复，本地可随时清空。

### 2.1 三层恢复向导

```
第 1 层  恢复源
   —— 云端（当前态为事实源）；若开启了时间点（P2）则选备份记录
第 2 层  勾选模块（相册 / 笔记 / 云盘，各自开关）
第 3 层  确认
   —— 明示"将覆盖本机 {勾选模块} 对应数据" → 执行
```

> 适配说明：OPPO 按"设备→备份记录"恢复，因它是设备态备份；
> 我们是**内容型备份**（云端即事实源），故第 1 层为"云端当前态"，时间点回滚列为 P2。

### 2.2 备份记录（轻量，P2 前仅作展示）

```sql
-- backup_records（可选表，P2 时间点恢复时启用）
CREATE TABLE backup_records (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,          -- auto / manual
  start_time TEXT, end_time TEXT,
  size INT,
  version TEXT,
  modules TEXT,                -- JSON: 覆盖模块及各自锚点(serverTime/anchor)
  created_at TEXT
);
```

- `auto`：每次成功同步生成一条（记录锚点）；`manual`：用户主动"创建备份点"
- 首版：仅记录展示"恢复历史"；**时间点回滚需数据版本化，列为 P2**

### 2.3 恢复执行

| 模块 | 恢复动作 |
|---|---|
| 相册 | 拉元数据（`/api/photos/sync`）→ 按需下载原图/缩略图 → 写回本地（MediaStore 可见） |
| 笔记 | 复用笔记客户端同步引擎（增量拉取，`/api/notes/sync`） |
| 云盘 | 需要时从 `/api/drive/download` 拉文件到本地（逐文件，Range 断点） |

- **断点续传 + 暂停/继续**：复用传输面板（`ChunkedUploader`/`ResumableDownloader`）
- **进度可视化**：按模块/按文件显示
- **覆盖语义**：仅勾选模块被覆盖，其余不动；恢复前提示

### 2.4 换机/重装入口

- 新设备配对后 → 恢复向导自动出现（或首页卡片"从云端恢复"）
- 本地已清空（符合"本地可丢"）→ 恢复即重建

## 三、服务端变更清单（my-cloud-server）

```
plugins/
├── photos/     # 补回收站表字段(deleted_at + 保留期) + 回收站路由 + 自动清除任务
├── drive/      # 现有回收站对齐统一 API 形状 + remainDays
└── notes/      # trash 对齐 remainDays 展示
core/
└── task_queue.py  # 注册"回收站超期清理"每日任务
config.py           # TRASH_RETAIN_DAYS = 30
```

## 四、客户端变更清单

| 端 | 变更 |
|---|---|
| Web（SPEC-WEB-FRONTEND） | 三模块回收站页（倒计时/恢复/彻底删/清空）+ 删除分级确认 + 恢复向导 |
| Android（SPEC-06 / 相册） | 回收站 UI + 恢复向导 + 离线倒计时缓存 |
| 通用 | 恢复向导流程（选择模块 → 确认覆盖 → 执行/进度/断点） |

## 五、完成定义

- [x] 三模块统一回收站：软删→30 天→自动清除；`remainDays` 返回（photos/drive/notes）
- [x] 删除分级确认（普通=告知进回收站，彻底=强警告）—— 前端交互已在回收站页实现强警告
- [x] 回收站超期自动清理任务（task_queue `trash.sweep`，每日 + 启动即跑）
- [ ] 恢复向导三层流程（源→勾选模块→确认覆盖→执行）—— 属客户端（Android/笔记 App）范围
- [ ] 恢复断点续传 + 暂停/继续 + 进度可视化 —— 客户端范围
- [ ] 换机/重装入口：配对后引导恢复 —— 客户端范围
- [ ] backup_records 记录（首版展示；时间点回滚 P2）—— 未启用（P2 前）
