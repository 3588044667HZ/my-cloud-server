"""photos 插件清单（Greenfield）。"""
import os

from config import DATA_DIR
from core.plugin_registry import Plugin
from plugins.photos.api import photos_bp


class PhotosPlugin(Plugin):
    name = 'photos'
    version = '0.1.0'
    stage = 'greenfield'    # 新建插件
    db_file = os.path.join(DATA_DIR, 'photos.db')
    migrations = 'migrations'
    tables = ('photos', 'uploads')
    routes = photos_bp

    def on_enable(self, core):
        pass

    def on_disable(self, core):
        pass

    def sweep_expired(self):
        """回收站超期自动清理（core/trash 分派）。"""
        from plugins.photos.api import sweep_expired as _sweep
        return _sweep()

    def sweep_uploads(self):
        """孤儿上传会话 TTL 清理（SPEC-UPLOAD-TRANSPORT §3.5）。"""
        from plugins.photos.api import sweep_uploads as _sweep
        return _sweep()


PLUGIN = PhotosPlugin()
