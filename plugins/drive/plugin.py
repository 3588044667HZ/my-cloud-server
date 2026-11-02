"""drive 插件清单（Greenfield）。"""
import os

from config import DATA_DIR, DRIVE_ENABLED_DEFAULT
from core.db import connect
from core.plugin_registry import Plugin
from plugins.drive.api import drive_bp
from plugins.drive.db import DRIVE_DB


class DrivePlugin(Plugin):
    name = 'drive'
    version = '0.1.0'
    stage = 'greenfield'    # 新建插件
    db_file = os.path.join(DATA_DIR, 'drive.db')
    migrations = 'migrations'
    tables = ('drive_settings', 'trash_index', 'drive_shares')
    routes = drive_bp

    def on_enable(self, core):
        # 云盘默认关闭（权限闸门默认关闭原则），首次建库写入默认开关值
        # 注意：on_enable 在请求上下文外执行，需用直接连接而非 get_conn(g)
        db = connect(DRIVE_DB)
        try:
            row = db.execute("SELECT value FROM drive_settings WHERE key = 'enabled'").fetchone()
            if row is None:
                db.execute("INSERT INTO drive_settings (key, value) VALUES ('enabled', ?)",
                           (DRIVE_ENABLED_DEFAULT,))
                db.commit()
        finally:
            db.close()

    def on_disable(self, core):
        pass

    def sweep_expired(self):
        """回收站超期自动清理（core/trash 分派）。"""
        from plugins.drive.api import sweep_expired as _sweep
        return _sweep()

    def sweep_uploads(self):
        """孤儿上传会话 TTL 清理（SPEC-UPLOAD-TRANSPORT §3.5）。"""
        from plugins.drive.api import sweep_uploads as _sweep
        return _sweep()


PLUGIN = DrivePlugin()
