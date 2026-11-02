"""插件注册表：扫描/注册/启停。

- 从 config.ENABLED_PLUGINS 加载；插件包在 plugins/<name>/plugin.py 暴露模块级 `PLUGIN`。
- 启用顺序：跑 alembic 迁移 → 注册任务处理器 → 注册路由 → on_enable(core)。
- 进程内协作：get_plugin(name) 供跨插件 service 调用（复制/引用语义）。
"""
import importlib
import importlib.util
import os

from config import DATA_DIR, ENABLED_PLUGINS
from core.db import ensure_tables, run_migrations


class Plugin:
    """插件契约（SERVER_SKELETON.md §2.1）。

    stage：演进阶段（ARCHITECTURE §5.1）—— legacy / adapter（既有实现兼容壳）
    / greenfield（新建）/ converged（彻底平权）。
    """

    name = ''
    version = '0.1.0'
    stage = 'converged'
    db_file = None          # 相对 data/ 的路径；None = 纯文件型
    migrations = None       # alembic 目录名
    tables = ()             # 关键表（ensure_tables 自愈校验用；可缺省）
    routes = None           # Blueprint，挂载到 /api 下
    task_handlers = {}      # dict[kind, Callable]

    def on_enable(self, core): ...
    def on_disable(self, core): ...


class Core:
    """传给插件 on_enable 的核心句柄（file_store / event_bus / task_queue / registry）。"""

    def __init__(self, registry):
        self.registry = registry
        self.file_store = None
        self.event_bus = None
        self.task_queue = None


class PluginRegistry:
    def __init__(self, enabled=None):
        self.enabled = enabled if enabled is not None else list(ENABLED_PLUGINS)
        self.plugins = {}          # name -> Plugin 实例
        self.core = Core(self)

    # ---------- 加载 ----------

    def load(self, app):
        """加载并启用全部插件；返回加载失败列表（不中断其它插件）。"""
        errors = []
        for name in self.enabled:
            try:
                mod = importlib.import_module(f'plugins.{name}.plugin')
                plugin = mod.PLUGIN
                self.plugins[name] = plugin
                self._enable(app, plugin)
            except Exception as e:  # noqa: BLE001 单插件失败不影响整体
                errors.append((name, str(e)))
        return errors

    def _enable(self, app, plugin):
        # 1) 迁移（含异常库自愈）
        if plugin.db_file and plugin.migrations:
            db_path = self.db_path(plugin.db_file)
            migrations_dir = self.migrations_dir(plugin)
            run_migrations(db_path, migrations_dir)
            ensure_tables(db_path, migrations_dir, set(plugin.tables or []))
        # 2) 任务处理器
        if plugin.task_handlers:
            for kind, handler in plugin.task_handlers.items():
                self.core.task_queue.register(kind, handler)
        # 3) 路由（单个 Blueprint 或 Blueprint 列表）
        routes = plugin.routes
        if routes is not None:
            for bp in (routes if isinstance(routes, (list, tuple)) else [routes]):
                app.register_blueprint(bp)
        # 4) 生命周期钩子
        plugin.on_enable(self.core)

    def disable(self, name):
        plugin = self.plugins.pop(name, None)
        if plugin:
            plugin.on_disable(self.core)

    # ---------- 路径 ----------

    def db_path(self, db_file):
        return db_file if db_file.startswith(DATA_DIR) else f'{DATA_DIR}/{db_file.lstrip("/")}'

    def migrations_dir(self, plugin):
        base = os.path.dirname(importlib.util.find_spec(f'plugins.{plugin.name}').origin)
        return os.path.join(base, 'migrations')

    # ---------- 进程内协作 ----------

    def get_plugin(self, name):
        return self.plugins.get(name)

    def plugin_enabled(self, name):
        return name in self.plugins
