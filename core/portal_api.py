"""portal 聚合入口 API：/api/portal/apps（SPEC-WEB-FRONTEND.md §5）。

应用宫格由服务端 portal_apps.json 配置驱动（可增删），默认缺省列表兜底。
"""
import json
import os

from flask import Blueprint, jsonify

from config import PORTAL_APPS_JSON

portal_bp = Blueprint('portal', __name__, url_prefix='/api/portal')

_DEFAULT_APPS = [
    {'id': 'album', 'name': '相册', 'icon': '', 'url': '/web/album', 'desc': '云端照片浏览与上传'},
    {'id': 'drive', 'name': '云盘', 'icon': '', 'url': '/web/drive', 'desc': '文件管理与回收站'},
    {'id': 'note', 'name': '便签', 'icon': '', 'url': '/web/note', 'desc': '跨设备同步便签'},
    {'id': 'admin', 'name': '管理后台', 'icon': '', 'url': '/admin', 'desc': '设备与硬盘管理'},
]


@portal_bp.route('/apps', methods=['GET'])
def apps():
    if os.path.isfile(PORTAL_APPS_JSON):
        try:
            with open(PORTAL_APPS_JSON, 'r', encoding='utf-8') as f:
                return jsonify({'code': 0, 'msg': 'success', 'data': json.load(f)})
        except (json.JSONDecodeError, OSError):
            pass
    return jsonify({'code': 0, 'msg': 'success', 'data': _DEFAULT_APPS})
