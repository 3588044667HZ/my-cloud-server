"""notes 插件清单（SERVER_SKELETON.md §2.1 Plugin 契约）。

独立库 data/notes.db + 独立 alembic 迁移 + 独立路由命名空间。
"""
import os

from config import DATA_DIR
from core.plugin_registry import Plugin
from plugins.notes.attachments_api import attachments_bp
from plugins.notes.notebooks_api import notebooks_bp
from plugins.notes.notes_api import notes_bp
from plugins.notes.share_settings import share_settings_bp


class NotesPlugin(Plugin):
    name = 'notes'
    version = '1.0.0'
    stage = 'adapter'       # 既有实现兼容壳（ARCHITECTURE §5.1）
    db_file = os.path.join(DATA_DIR, 'notes.db')
    migrations = 'migrations'
    tables = ('notebooks', 'notes', 'share_settings', 'attachments')
    routes = [notes_bp, notebooks_bp, attachments_bp, share_settings_bp]

    def on_enable(self, core):
        pass

    def on_disable(self, core):
        pass

    def sweep_expired(self):
        """回收站超期自动清理（core/trash 分派）。"""
        from plugins.notes.notes_api import sweep_expired as _sweep
        return _sweep()


PLUGIN = NotesPlugin()
