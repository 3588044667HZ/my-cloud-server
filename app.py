"""Flask 工厂 + Waitress 入口。

启动顺序：core.db 迁移 → CORS → 全局设备守卫 → devices/admin 路由 →
插件注册表（notes/photos/drive）→ 任务队列 worker。
"""
import os

from flask import Flask, jsonify
from flask_cors import CORS

from config import DATA_DIR, ENABLED_PLUGINS, MAX_CONTENT_LENGTH, PLACEHOLDER_IMAGE_PATH
from core.auth.devices import devices_bp, register_device_guard
from core.bootstrap import init_core_db
from core.db import close_all
from core.plugin_registry import PluginRegistry
from core.task_queue import start as start_task_queue


def create_app(test_config=None):
    os.makedirs(DATA_DIR, exist_ok=True)
    init_core_db()

    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH
    CORS(app)

    @app.route('/api/health')
    def health():
        return jsonify({'status': 'ok'})

    # 全局设备鉴权守卫 + 设备路由
    register_device_guard(app)
    app.register_blueprint(devices_bp)

    # 管理后台
    from admin.admin_api import admin_bp
    app.register_blueprint(admin_bp)

    # portal 聚合入口（/api/portal/apps）
    from core.portal_api import portal_bp
    app.register_blueprint(portal_bp)

    # 管理后台前端（/admin，Vue3 SPA）
    from admin.frontend import register_admin_frontend
    register_admin_frontend(app)

    # 笔记前端同源托管（UA 分发 desktop/mobile）
    from core.frontend import register_frontend
    register_frontend(app)

    # 插件注册表（notes / photos / drive）
    registry = PluginRegistry(enabled=(test_config or {}).get('ENABLED_PLUGINS') or ENABLED_PLUGINS)

    # 先组装 core 句柄（供插件 on_enable / task_handlers 注册使用），再加载插件
    from core import event_bus, file_store, task_queue
    registry.core.file_store = file_store
    registry.core.event_bus = event_bus
    registry.core.task_queue = task_queue
    registry.core.registry = registry

    load_errors = registry.load(app)

    # 事件订阅（ARCHITECTURE §5.2 事件模式）：photo.uploaded → 记录上传计数，供管理后台展示
    def _on_photo_uploaded(payload):
        from config import CORE_DB
        from core.db import get_conn
        db = get_conn(CORE_DB)
        row = db.execute("SELECT value FROM app_config WHERE key = 'stats_photo_uploads'").fetchone()
        val = (int(row['value']) + 1) if row else 1
        db.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES ('stats_photo_uploads', ?)",
                   (str(val),))
        db.commit()
    event_bus.subscribe('photo.uploaded', _on_photo_uploaded)

    # 任务队列 worker
    if (test_config or {}).get('TASK_QUEUE_ENABLED', True):
        start_task_queue()
        # 注册并调度 GC（启动时回收一次孤儿文件，ARCHITECTURE §5.5/§7.3）
        task_queue.register('file.gc', file_store.gc_task_handler)
        file_store.schedule_gc()
        # 回收站超期自动清理（SPEC-CLOUD-RECYCLE-RECOVERY.md §1.1：每天一次）
        from core import trash
        trash.set_registry(registry)
        task_queue.register('trash.sweep', trash.sweep_task_handler)
        task_queue.register('upload.sweep', trash.upload_sweep_task_handler)
        trash.schedule_sweep()

    # 占位图兜底生成
    _ensure_placeholder()

    app.teardown_appcontext(close_all)
    app.extensions['plugin_registry'] = registry
    app.config['PLUGIN_LOAD_ERRORS'] = load_errors
    return app


def _ensure_placeholder():
    """服务端内置占位图不存在时生成灰色 WEBP。"""
    if os.path.isfile(PLACEHOLDER_IMAGE_PATH):
        return
    try:
        from PIL import Image
        import io
        os.makedirs(os.path.dirname(PLACEHOLDER_IMAGE_PATH), exist_ok=True)
        img = Image.new('RGB', (480, 360), (235, 235, 235))
        buf = io.BytesIO()
        img.save(buf, 'WEBP', quality=70)
        with open(PLACEHOLDER_IMAGE_PATH, 'wb') as f:
            f.write(buf.getvalue())
    except Exception:
        pass


def main():
    from waitress import serve
    app = create_app()
    print(' * my-cloud-server: http://0.0.0.0:5000')
    serve(app, host='0.0.0.0', port=5000)


if __name__ == '__main__':
    main()
