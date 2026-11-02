"""管理后台前端托管：/admin 及子路径 → static/admin（Vue3 SPA，hash 路由）。

前端构建输出：npm run build（admin-frontend/）→ ../static/admin
"""
import os
from flask import abort, send_from_directory

STATIC_ADMIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'admin')


def register_admin_frontend(app):
    """注册 /admin SPA 路由；前端未构建时该入口 404（不影响 API）。"""
    if not os.path.isdir(STATIC_ADMIN):
        return

    @app.route('/admin', defaults={'path': ''}, strict_slashes=False)
    @app.route('/admin/<path:path>')
    def serve_admin(path):
        if not path:
            return send_from_directory(STATIC_ADMIN, 'index.html')
        full = os.path.join(STATIC_ADMIN, path)
        if os.path.isfile(full):
            return send_from_directory(STATIC_ADMIN, path)
        # SPA fallback：非静态资源请求一律回 index.html
        return send_from_directory(STATIC_ADMIN, 'index.html')
