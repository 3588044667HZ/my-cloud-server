# my-cloud-server 项目骨架与搭建设计

> 状态：设计定稿，待 agent 在 `E:\PyCharmProjects\my-cloud-server` 实现
> 技术栈：**Python + Flask + Waitress**（与 mock-server 一致）
> 定位：统一云服务服务端，替代 mock-server（mock-server 冻结为 legacy）
> 关联设计：`mock-server/ARCHITECTURE.md`、`mock-server/UNIFIED_DEVICE_TOKEN_AUTH_SPEC.md`
> 本文件是项目搭建设计依据；代码由 agent 实现。

## 1. 目录结构

```
E:\PyCharmProjects\my-cloud-server\
├── app.py                       # Flask 工厂 + Waitress 入口
├── config.py                    # 全局配置（单用户 id、PHOTOS_DIR、DRIVE_SHARES、
│                                #   HIDDEN_BACKUP_DIRS、配对码 TTL 等）
├── requirements.txt             # flask, flask-cors, waitress, pillow, alembic, gunicorn(备用)
├── core/                        # 核心平台（所有插件共享，不随插件拆分）
│   ├── __init__.py
│   ├── protocol.py              # success/error/paginate 响应工具，错误码表
│   ├── auth/
│   │   ├── __init__.py          # require_device 中间件
│   │   ├── devices.py           # /api/devices/* 路由
│   │   └── devices.sql          # devices 表 DDL（见 SPEC §2.1）
│   ├── plugin_registry.py       # 插件扫描/注册/启停（config 指定启用列表）
│   ├── db.py                    # 插件 SQLite 生命周期（连接/迁移/关闭）
│   ├── file_store.py            # 内容寻址 md5 + 引用计数 + 统一 GC
│   ├── event_bus.py             # 进程内发布/订阅
│   └── task_queue.py            # SQLite 任务队列 + 单 worker 线程
├── plugins/                     # 各领域插件（每插件独立库/迁移/路由）
│   ├── notes/                   # 笔记插件（从 mock-server 迁入，功能等价）
│   │   ├── __init__.py
│   │   ├── plugin.py            # 实现 core 的 Plugin 契约
│   │   ├── migrations/          # alembic
│   │   ├── notes_api.py         # 笔记 CRUD + sync + 回收站 + restore
│   │   ├── notebooks_api.py
│   │   ├── attachments_api.py   # 两阶段上传 + 缩略图（沿用 mock-server 逻辑）
│   │   ├── share_settings.py
│   │   └── schema.sql           # notes/notebooks/attachments/share_settings 表
│   ├── photos/                  # 相册插件（Greenfield）
│   │   ├── __init__.py
│   │   ├── plugin.py
│   │   ├── migrations/
│   │   ├── api.py               # register / upload(分片) / sync / download / thumb
│   │   ├── thumb.py             # Pillow 缩略图（复用 mock-server 的 THUMB 规格）
│   │   └── schema.sql           # photos / 文件引用
│   └── drive/                   # 云盘插件（Greenfield）
│       ├── __init__.py
│       ├── plugin.py
│       ├── migrations/
│       ├── resolver.py          # ShareResolver（ConfigShareResolver / RemoteSmbResolver）
│       ├── api.py               # shares/list/mkdir/rename/move/copy/delete/trash
│       ├── stream.py            # 分片上传 / Range 下载
│       └── schema.sql           # drive_settings / 回收站索引
├── admin/                       # 管理后台（设备管理/配对码/孤儿清理）
│   ├── __init__.py
│   └── admin_api.py             # /api/admin/*：设备列表/生成配对码/撤销/清理
├── scripts/
│   ├── gen_pair_code.py         # 终端生成配对码
│   └── migrate_from_mock.py     # 从 mock-server 迁移存量数据
├── data/                        # 运行时数据（.gitignore）
│   ├── core.db                  # devices 等核心表
│   ├── notes.db                 # 迁移自 mock-server
│   ├── photos.db
│   ├── drive.db
│   └── files/                   # 附件/相册文件（分桶存储）
└── tests/                       # pytest：core + 各插件 + 迁移
```

## 2. 关键设计约定

### 2.1 Plugin 契约（core/plugin_registry.py 定义）

```python
class Plugin:
    name: str
    version: str
    db_file: str | None        # 相对 data/ 的路径；None = 纯文件型
    migrations: str            # alembic 目录名
    routes: Blueprint          # 挂载到 /api 下
    task_handlers: dict[str, Callable]
    def on_enable(self, core): ...
    def on_disable(self, core): ...
```

注册表从 `config.py` 的 `ENABLED_PLUGINS` 加载；插件 `plugin.py` 暴露模块级 `PLUGIN = <Plugin 实例>`。

### 2.2 鉴权（实现 `UNIFIED_DEVICE_TOKEN_AUTH_SPEC.md`）

- `core/auth` 提供 `require_device`，全部 `/api/*`（除 `/api/devices/pair*`、`/health`）套用
- token 只存 SHA-256；`g.device` / `g.user_id`（单用户常量，来自 `config.py: SINGLE_USER_ID`）
- `/api/devices/pair|pair-code|me|list|delete` 完整实现；**无 `/api/auth/*`**

### 2.3 文件层（core/file_store.py）

- `put(path_or_bytes) -> file_id`（= md5，内容寻址，分桶 `{PHOTOS_DIR}/{ab}/{cd}/{file_id}`）
- `ref(file_id)` / `unref(file_id)`（引用计数）；`gc()`（批量回收 ref_count==0）
- 所有插件的文件读写统一走 file_store，跨插件去重

### 2.4 事件/队列（轻量）

- `core/event_bus.py`：`publish(topic, payload)` + `subscribe(topic, handler)`，进程内
- `core/task_queue.py`：SQLite `tasks` 表 + 单 worker 线程；插件注册 `task_handlers`
  执行缩略图/分片合并/GC 等耗时操作

## 3. notes 插件：从 mock-server 迁入（功能等价，非重写）

| mock-server 文件 | 迁入 my-cloud-server 位置 | 改动 |
|---|---|---|
| `app/auth.py` | **删除** | 鉴权统一走 core/auth 设备 token；无用户注册/登录 |
| `app/notes.py` | `plugins/notes/notes_api.py` | 表逻辑不变；`g.user_id` 由 require_device 注入 |
| `app/notebooks.py` | `plugins/notes/notebooks_api.py` | 同上 |
| `app/files.py` | `plugins/notes/attachments_api.py` | 两阶段上传逻辑保留；文件落盘改走 `core/file_store` 或保留原目录（见 §5） |
| `app/settings.py` | `plugins/notes/share_settings.py` | 不变 |
| `app/database.py` | `plugins/notes/schema.sql` + `core/db.py` | 表结构原样保留 |
| `app/admin_api.py` | `admin/admin_api.py` | 用户管理改为**设备管理** |
| `config.py`（THUMB/上传限制等） | `config.py` | 常量搬移 |

**功能等价标准**：现有三端（Android / 移动 Web / 桌面 Web）切到 my-cloud-server 后，
笔记 CRUD、增量同步、附件两阶段、回收站、分享设置、RESTORE 冲突恢复行为完全一致
（仅鉴权换为设备 token）。

## 4. 数据迁移（scripts/migrate_from_mock.py）

```
1. 复制 mock-server/data/notes.db            → my-cloud-server/data/notes.db
2. 复制 mock-server/data/accounts.db          → 仅取单用户行，写入 core 配置 SINGLE_USER_ID
3. 复制 mock-server/data/files/*              → my-cloud-server/data/files/（附件）
4. 复制 mock-server/backup/                   → my-cloud-server/backup/（txt 冷备）
5. 生成首台设备配对码：python scripts/gen_pair_code.py
```

- 迁移脚本**只读 mock-server，只写 my-cloud-server**，可重复执行（幂等）
- 迁移后三端把 server_url 指向 my-cloud-server，重新配对即可使用

## 5. 文件落盘（机械盘多盘拓扑）

```
config.py:
  PHOTOS_DIR   = 'D:/photos'       # 3T 盘：相册 orig/thumb 分桶
  DATA_DIR     = 'C:/my-cloud-server/data'   # 本地盘：SQLite + 附件
  ATTACH_DIR   = '{DATA_DIR}/files'
  DRIVE_SHARES = [                 # 云盘对外共享白名单（配置即白名单）
      {'share': 'DATA3T',  'local_path': 'D:\\', 'label': '3T 数据盘', 'enabled': True},
      {'share': 'DATA500A','local_path': 'E:\\', 'label': '500G-1',    'enabled': True},
      {'share': 'DATA500B','local_path': 'F:\\', 'label': '500G-2',    'enabled': True},
      {'share': 'SYSTEM',  'local_path': 'C:\\', 'label': '系统盘',     'enabled': False},
  ]
  HIDDEN_BACKUP_DIRS = ['D:/云备份']   # 云盘默认隐藏的备份目录
  云盘浏览时默认隐藏 HIDDEN_BACKUP_DIRS（"显示备份目录"开关）
```

首版附件可沿用 `data/files/` 目录（迁移零改动）；file_store 统一为**远期收敛**目标，
不阻塞首版。

## 6. 管理后台（admin）

- `/api/admin/*` 复用 mock-server 的 ADMIN_PASSWORD 鉴权方式
- 新增：**设备管理**（列表/重命名/撤销）、**生成配对码**、**孤儿设备清理**
  （`last_active` 超 30 天提示回收）
- 终端 `gen_pair_code.py` 与后台生成等价

## 7. 实现顺序（server agent 任务链）

1. **core 平台**：protocol → auth(devices) → plugin_registry → db → file_store →
   event_bus → task_queue
2. **notes 插件迁入**：功能等价，`/api/notes/*` 全量通过三端联调
3. **photos 插件**：register/分片上传/缩略图/sync/download（curl 可验证）
4. **drive 插件**：ShareResolver → 文件操作全套 + 回收站 + Range
5. **admin 设备管理** + 配对码 + 孤儿清理
6. **迁移脚本** + 部署脚本（可复用 mock-server 的 TAE 机制）

## 8. 测试策略

- `tests/` 用 pytest + Flask test client，每插件独立测试文件
- 重点：配对/撤销/401；notes 增量同步与冲突语义；photos 分片幂等；drive 路径沙箱
- 迁移脚本幂等性测试（跑两遍结果一致）

## 9. 完成定义

- [ ] core 平台 + 插件注册表可加载 notes/photos/drive
- [ ] notes 功能等价（三端联调通过，仅鉴权变设备 token）
- [ ] photos/drive 基础接口 curl 可验证
- [ ] 管理后台设备管理 + 配对码
- [ ] 迁移脚本幂等；mock-server 数据可完整迁入
- [ ] 部署：Waitress 启动，TAE 或直接脚本托管
