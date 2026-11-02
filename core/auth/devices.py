"""/api/devices/* 路由 + 全局设备鉴权守卫。

端点（UNIFIED_DEVICE_TOKEN_AUTH_SPEC §2.2）：
  POST   /api/devices/pair            # 配对码 → 设备 token（免鉴权）
  POST   /api/devices/pair-code       # 生成一次性配对码（需已授权设备 / 管理后台）
  GET    /api/devices                 # 设备列表
  GET    /api/devices/me              # 校验当前 token
  DELETE /api/devices/<device_id>     # 撤销设备
"""
from flask import Blueprint, g, jsonify, request

from config import CORE_DB
from core.auth import (
    consume_pair_code, generate_pair_code, lookup_device, now_iso,
    parse_bearer, register_device, touch_device, utc_ts,
)
from core.db import get_conn
from core.protocol import error, success

devices_bp = Blueprint('devices', __name__, url_prefix='/api/devices')

# 免设备鉴权的路径规则：(method, path_prefix) 或 ('*', path_prefix)
DEVICE_AUTH_EXEMPT = [
    ('POST', '/api/devices/pair'),
    ('*', '/api/health'),
]
ADMIN_PREFIX = '/api/admin/'

# 附件/相册/云盘缩略图与下载允许匿名（浏览器 <img> 场景），但带 token 则必须有效
_DOWNLOAD_PREFIXES = ('/api/attachments/', '/api/photos/')
_DRIVE_ANON_PREFIXES = ('/api/drive/download', '/api/drive/thumb')


def _is_exempt(method, path):
    """完全免鉴权（pair/health）。附件/相册/云盘下载等可选鉴权不在此列。"""
    for m, prefix in DEVICE_AUTH_EXEMPT:
        if (m == '*' or m == method) and path.startswith(prefix):
            return True
    return False


def _is_admin(path):
    return path.startswith(ADMIN_PREFIX)


def _is_optional_auth(path):
    if any(path.startswith(p) and path.endswith('/download') for p in _DOWNLOAD_PREFIXES):
        return True
    # drive 下载/缩略图以 query 参数定位（path=...），前缀匹配即可
    if any(path.startswith(p) for p in _DRIVE_ANON_PREFIXES):
        return True
    return False


def register_device_guard(app):
    """全局守卫：/api/* 除白名单外一律要求有效设备 token。"""
    from flask import g

    @app.before_request
    def device_guard():
        path = request.path
        method = request.method
        if not path.startswith('/api/'):
            return None
        if _is_admin(path):
            return None

        token = parse_bearer()
        if _is_exempt(method, path):
            return None                       # 完全免鉴权（pair/health）
        if _is_optional_auth(path) and not token:
            return None                       # 匿名放行（附件/相册下载、云盘缩略图）；带 token 则校验

        dev = lookup_device(token)
        if dev is None:
            return jsonify({'code': 10401, 'msg': 'unauthorized', 'data': None}), 401
        touch_device(dev['device_id'])
        g.device = dev
        g.user_id = dev['user_id']
        return None


def _device_row_to_dict(r):
    return {
        'deviceId': r['device_id'],
        'name': r['name'],
        'createdAt': r['created_at'],
        'lastActive': r['last_active'],
        'revokedAt': r['revoked_at'],
    }


@devices_bp.route('/pair', methods=['POST'])
def pair():
    data = request.get_json(silent=True) or {}
    code = data.get('code', '')
    if not consume_pair_code(code):
        return error('Invalid or expired pair code', code=10401, status=401)
    device_id = (data.get('deviceId') or '').strip() or None
    name = (data.get('name') or '').strip()
    return success(register_device(device_id=device_id, name=name))


@devices_bp.route('/pair-code', methods=['POST'])
def pair_code():
    return success({
        'code': generate_pair_code(),
        'expiresIn': 600,
    })


@devices_bp.route('', methods=['GET'])
def list_devices():
    db = get_conn(CORE_DB)
    rows = db.execute('SELECT * FROM devices ORDER BY created_at ASC').fetchall()
    return success([_device_row_to_dict(r) for r in rows])


@devices_bp.route('/me', methods=['GET'])
def me():
    from core.auth import get_single_user
    user = get_single_user()
    return success({
        'deviceId': g.device['device_id'],
        'name': g.device['name'],
        'createdAt': g.device['created_at'],
        'lastActive': g.device['last_active'],
        'user': {
            'id': g.device['user_id'],
            'name': user['name'],
        },
    })


@devices_bp.route('/<device_id>', methods=['DELETE'])
def revoke(device_id):
    db = get_conn(CORE_DB)
    row = db.execute('SELECT * FROM devices WHERE device_id = ?', (device_id,)).fetchone()
    if not row:
        return error('Device not found', code=10404, status=404)
    db.execute(
        'UPDATE devices SET revoked_at = ? WHERE device_id = ?',
        (now_iso(), device_id)
    )
    db.commit()
    return success(None)
