"""前端同源托管：聚合页在 index，笔记前端仅经聚合页进入。

- 聚合页：`/` 与 `/portal` 均返回 portal.html（SPEC-WEB-FRONTEND.md §1）。
- 笔记前端：仅 `/web/note`（及 /assets/* 资源）可达，按 UA 分发 desktop/mobile——
  `GET /` 不再直接分发笔记首页，笔记只能从聚合页点应用宫格进入。
- 云服务子应用：/web/album 相册 · /web/drive 云盘 · /web/note 笔记。
- SPA fallback；排除 /api/ 与 /admin/ 前缀（避免劫持 API/管理后台路由）。
"""
import os

from flask import abort, g, request, send_from_directory

from config import MOBILE_UA_KEYWORDS, STATIC_DESKTOP, STATIC_MOBILE, STATIC_WEB


def detect_device():
    ua = request.user_agent.string.lower()
    g.is_mobile = any(kw in ua for kw in MOBILE_UA_KEYWORDS)


def _send_web(file_name):
    return send_from_directory(STATIC_WEB, file_name)


def _serve_note(subpath):
    """笔记前端：按 UA 分发 static/mobile 或 static/desktop，SPA fallback。"""
    base = STATIC_MOBILE if g.get('is_mobile') else STATIC_DESKTOP
    if not os.path.isdir(base):
        return abort(404)
    subpath = (subpath or '').lstrip('/')
    if not subpath:
        return send_from_directory(base, 'index.html')
    full = os.path.join(base, subpath)
    if os.path.isfile(full):
        return send_from_directory(base, subpath)
    return send_from_directory(base, 'index.html')


def register_frontend(app):
    app.before_request(detect_device)

    # ---------- 聚合页：/ 与 /portal ----------
    if os.path.isdir(STATIC_WEB):
        @app.route('/', methods=['GET'])
        def index():
            return _send_web('portal.html')

        @app.route('/portal', defaults={'path': ''})
        @app.route('/portal/<path:path>')
        def serve_portal(path):
            if not path:
                return _send_web('portal.html')
            if path == 'pair.html':
                return _send_web('pair.html')
            full = os.path.join(STATIC_WEB, path)
            if os.path.isfile(full):
                return send_from_directory(STATIC_WEB, path)
            return _send_web('portal.html')

        # ---------- 云服务子应用 ----------
        @app.route('/web/', defaults={'path': 'portal.html'})
        @app.route('/web/<path:path>')
        def serve_web(path):
            full = os.path.join(STATIC_WEB, path)
            if os.path.isfile(full):
                return send_from_directory(STATIC_WEB, path)
            # SPA fallback：按子应用回对应页面
            if path.startswith('album'):
                return _send_web('album.html')
            if path.startswith('drive'):
                return _send_web('drive.html')
            return _send_web('portal.html')

    # ---------- 笔记前端（仅 /web/note 进入） ----------
    @app.route('/web/note', defaults={'path': ''})
    @app.route('/web/note/<path:path>')
    def serve_note(path):
        return _serve_note(path)

    # ---------- 笔记前端静态资源（/assets/* 等真实文件） ----------
    # 未知路径一律 404：笔记只能经 /web/note 进入，不提供任意路径的 SPA 兜底
    @app.route('/<path:path>')
    def catch_all(path):
        if path.startswith(('api/', 'admin/', 'web/', 'portal/')):
            return abort(404)
        base = STATIC_MOBILE if g.get('is_mobile') else STATIC_DESKTOP
        if not os.path.isdir(base):
            return abort(404)
        full = os.path.join(base, path)
        if os.path.isfile(full):
            return send_from_directory(base, path)
        return abort(404)
