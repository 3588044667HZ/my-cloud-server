# 云服务导流界面设计（my-cloud-server 静态页 · 纯导流）

> 状态：设计定稿，待 agent 实现
> 目标读者：前端 agent（Vue3）+ 服务端 agent（静态页路由/配置接口）
> 关联：`UNIFIED_DEVICE_TOKEN_AUTH_SPEC.md`（设备 token）、`ARCHITECTURE.md`、
> `E:\oppo-cloud-rev\spec-云服务导流界面设计.md`（OPPO 界面视觉还原参考）
> 形态：**纯导流**（欢迎区 + 我的应用宫格），不展示云端数据、无数据卡片。

## 1. 目标与形态

- 仿 OPPO 云空间首页：上半欢迎区 + 「我的应用」宫格（子应用入口，点击跳转）
- **纯导流**：宫格项只做跳转，不拉取云端数据（相册/笔记/待办数据卡片**不做**）
- 同源部署于 `my-cloud-server`，与后端同源 → 天然共享 localStorage 登录态

```
云服务首页（/portal/index.html）
├── 欢迎区：头像 + "你好，{name}"（GET /api/devices/me）
├── 我的应用宫格：6列×2行=12/页，可翻页
│   └── 每项：图标64x64 + 名称 → 点击跳转子应用
└── （无数据卡片）
```

## 2. 认证设计（核心）

### 2.1 原则：网页 = 一个"浏览器设备"

完全复用 `UNIFIED_DEVICE_TOKEN_AUTH_SPEC.md` 的设备 token 体系，**无新机制**。

| 项 | 值 |
|---|---|
| 凭证 | 设备 token（localStorage: `device_token`） |
| 获取 | 配对页输入配对码 → `POST /api/devices/pair` |
| 校验 | `GET /api/devices/me`（对应 OPPO `token/v1/status`） |
| 失效 | 401 → 清 token → 回配对页 |

### 2.2 认证流程

```
打开 /portal/
  ├─ localStorage 无 device_token ──► 跳 /portal/pair.html
  ├─ 有 token ──► 并行：
  │     GET /api/devices/me        → 成功：欢迎区显示 name，进首页
  │     （失败 401）→ 清 token → 跳 pair.html
  └─ pair.html：输入配对码 → POST /api/devices/pair
        → 存 localStorage → 跳回 index.html
```

### 2.3 401 统一拦截（防死循环）

```ts
// 所有请求响应拦截器
if (code === 10401 || http 401) {
  if (!sessionStorage.getItem('redirecting')) {   // 防重定向死循环
    sessionStorage.setItem('redirecting', '1')
    localStorage.removeItem('device_token')
    location.href = '/portal/pair.html?next=' + encodeURIComponent(location.href)
  }
}
```

### 2.4 简化的项（对比 OPPO）

| OPPO 网页版 | 本设计 | 原因 |
|---|---|---|
| `ctoken/TOKENKEY` + AES 加密 | 仅设备 token + HTTPS | 单用户 + 内网（Tailscale），无第三方风险 |
| `trustedDevice` 锁定态 + 二次验证 | **省略** | 单用户，配对码已是最强门槛 |
| 15s 轮询 `token/status` | 仅进入时校验一次 | 纯导流无敏感数据，无需轮询 |
| WebSocket 在线 | **省略** | 无跨端文件卡片 |

### 2.5 同源跳转：token 天然共享

子应用若同为 `my-cloud-server` 下的网页（同源），共享同一个
localStorage `device_token`，**登录态自动互通**，跳转无需额外参数。
（仅当子应用为外部/第三方时才需另配 SSO——不在本设计范围。）

## 3. 页面结构与视觉 token

> 视觉还原自 OPPO 导流 spec（`E:\oppo-cloud-rev\spec-云服务导流界面设计.md` §4.5/§9）。

```
┌────────────────────────────────────────┐
│ 顶栏: logo · [语言▾] [区域▾] [头像]      │
├────────────────────────────────────────┤
│ 欢迎区                                  │
│   (80x80 头像) 你好, {name}             │
├────────────────────────────────────────┤
│ 我的应用                                │
│ ┌────┐ ┌────┐ ┌────┐ ┌────┐ ┌────┐ ┌────┐
│ │图标 │ │图标 │ │图标 │ │图标 │ │图标 │ │图标 │  6列×2行
│ │名称 │ │名称 │ │名称 │ │名称 │ │名称 │ │名称 │  每页12
│ └────┘ └────┘ └────┘ └────┘ └────┘ └────┘
│ ┌────┐ ┌────┐ ┌────┐ ┌────┐ ┌────┐ ┌────┐
│ │    │ │    │ │    │ │    │ │    │ │    │
│ └────┘ └────┘ └────┘ └────┘ └────┘ └────┘
│            ● ○   （翻页圆点）
├────────────────────────────────────────┤
│ 页脚                                    │
└────────────────────────────────────────┘
```

| Token | 值 |
|---|---|
| 页面容器 | 984px 居中 |
| 宫格容器 | 648×312，圆角 26px，玻璃拟态 `linear-gradient(180deg,rgba(255,255,255,.192),rgba(255,255,255,.064))`，边框 `rgba(255,255,255,.6)`，阴影 `0 24px 32px rgba(0,0,0,.1)` |
| 网格 | 6 列；项宽 79px、高 110px；右距 28px（`nth-child(6n)` 清 0） |
| 图标 | 64×64；hover 背景 `rgba(0,0,0,.06)`、圆角 20px |
| 名称 | 14px，色 `rgba(0,0,0,.9)` |
| 翻页 | `translateX(-100%*current)` 0.3s；底部 6px 圆点；移动端 touch 滑动 |
| 主按钮蓝 | `#06f` |

## 4. 宫格数据源（配置驱动）

宫格内容由服务端配置提供，新增子应用不改前端：

```
GET /api/portal/apps        # 无需鉴权（纯导航清单）
  resp: { code:0, data: [ { id, name, icon, url, order } ] }
```

服务端配置示例（`config.py` 或 `portal_apps.json`）：

```json
[
  { "id": "note",   "name": "便签",     "icon": "/portal/icons/note.png",   "url": "/web/note" },
  { "id": "drive",  "name": "云盘",     "icon": "/portal/icons/drive.png",  "url": "/web/cloud" },
  { "id": "admin",  "name": "管理后台",  "icon": "/portal/icons/admin.png",  "url": "/admin" }
]
```

- `url` 同源相对路径（共享 token）；跳转直接 `location.href = url`
- 图标缺失时 fallback 到默认图标

## 5. 部署位置（my-cloud-server）

```
my-cloud-server/
├── static/portal/
│   ├── index.html        # 导流首页
│   ├── pair.html         # 配对页
│   └── assets/           # Vue3 构建产物 / 图标
└── app.py / 插件         # 挂载静态路由: GET /portal 及其静态资源
```

- 静态页由 Flask 直接服务（参考 mock-server `static/desktop` 的 UA 分发机制）
- `GET /api/portal/apps` 由服务端提供（读 `portal_apps.json`）
- 配对接口 `POST /api/devices/pair` 已有（`UNIFIED_DEVICE_TOKEN_AUTH_SPEC.md` §2）

## 6. 目录结构（前端）

```
src/
├─ api/portal.ts        // apps 清单、devices/me
├─ stores/auth.ts       // device_token、pair、401 拦截
├─ views/
│  ├─ PortalHome.vue    // 欢迎区 + 宫格
│  └─ PairPage.vue      // 配对码输入
└─ components/
   ├─ ApplyGrid.vue     // 宫格 + swiper 翻页
   ├─ ApplyItem.vue
   └─ WelcomeDetail.vue
```

## 7. 完成定义

- [ ] `/portal` 静态页可访问；未配对 → 跳 `pair.html`
- [ ] `pair.html` 配对 → 存 token → 回首页，欢迎区显示设备/用户名
- [ ] 宫格从 `GET /api/portal/apps` 渲染；点击跳转 `url`
- [ ] 401 统一拦截 → 清 token → 跳配对（`redirecting` 防死循环）
- [ ] 同源子应用（笔记/云盘/管理后台）跳转后登录态互通
- [ ] 视觉符合 §3 token（玻璃拟态/6列/翻页/移动端 touch）
