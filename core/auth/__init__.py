"""设备 token 鉴权核心：require_device / parse_bearer / lookup_device / 单用户 / 配对码。

token 只存 SHA-256；g.device / g.user_id（单用户常量，来自 core.db app_config）。
"""
import base64
import hashlib
import secrets
import uuid

from flask import g, request

from config import CORE_DB, PAIR_CODE_TTL, PAIR_CODE_ALPHABET, SINGLE_USER_ID, SINGLE_USER_NAME
from core.db import get_conn
from core.util import now_iso, utc_ts


# ---------- 单用户 ----------

def get_single_user():
    """读取单用户身份（core.db app_config），缺省回退 config 常量。"""
    db = get_conn(CORE_DB)
    def _get(key, default):
        row = db.execute('SELECT value FROM app_config WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else default
    return {
        'user_id': _get('single_user_id', SINGLE_USER_ID),
        'name': _get('single_user_name', SINGLE_USER_NAME),
    }


def set_single_user(user_id, name):
    """写入单用户身份（迁移脚本调用）。"""
    db = get_conn(CORE_DB)
    for key, value in (('single_user_id', str(user_id)), ('single_user_name', name)):
        db.execute('INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)', (key, value))
    db.commit()


# ---------- token ----------

def generate_token():
    """32 字节密码学随机数，base64url 编码。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode('ascii')


def hash_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def parse_bearer(headers=None):
    """从 Authorization 头解析 token。"""
    headers = headers if headers is not None else request.headers
    header = headers.get('Authorization', '')
    return header[7:] if header.startswith('Bearer ') else ''


def lookup_device(token):
    """按 token 哈希查设备；不存在/已撤销返回 None。"""
    if not token:
        return None
    db = get_conn(CORE_DB)
    row = db.execute(
        'SELECT * FROM devices WHERE token_hash = ? AND revoked_at IS NULL',
        (hash_token(token),)
    ).fetchone()
    return dict(row) if row else None


def touch_device(device_id):
    """每次请求刷新 last_active。"""
    db = get_conn(CORE_DB)
    db.execute('UPDATE devices SET last_active = ? WHERE device_id = ?', (now_iso(), device_id))
    db.commit()


def require_device(fn):
    """装饰器：校验设备 token，注入 g.device / g.user_id；失败 401 10401。

    也可直接用于全局 before_request（见 core/auth/devices.py 中注册的守卫）。
    """
    from functools import wraps
    from flask import jsonify

    @wraps(fn)
    def wrapper(*args, **kwargs):
        dev = lookup_device(parse_bearer())
        if dev is None:
            return jsonify({'code': 10401, 'msg': 'unauthorized', 'data': None}), 401
        touch_device(dev['device_id'])
        g.device = dev
        g.user_id = dev['user_id']
        return fn(*args, **kwargs)
    return wrapper


# ---------- 配对码 ----------

def generate_pair_code():
    """生成 4+4 配对码（去易混字符），落库并返回。"""
    rng = secrets.SystemRandom()
    def _seg(n):
        return ''.join(rng.choice(PAIR_CODE_ALPHABET) for _ in range(n))
    code = f'{_seg(4)}-{_seg(4)}'
    db = get_conn(CORE_DB)
    db.execute(
        'INSERT OR REPLACE INTO pair_codes (code, expires_at, used, created_at) VALUES (?, ?, 0, ?)',
        (code, str(utc_ts() + PAIR_CODE_TTL), now_iso())
    )
    db.commit()
    return code


def consume_pair_code(code):
    """校验并一次性消费配对码。成功返回 True，失败（过期/不存在/已用）返回 False。"""
    if not code:
        return False
    db = get_conn(CORE_DB)
    row = db.execute('SELECT * FROM pair_codes WHERE code = ?', (code.strip().upper(),)).fetchone()
    if not row:
        return False
    if row['used'] or int(row['expires_at']) < utc_ts():
        return False
    db.execute('UPDATE pair_codes SET used = 1 WHERE code = ?', (row['code'],))
    db.commit()
    return True


def generate_device_id():
    """服务端兜底生成 deviceId（客户端未提供时）。"""
    return str(uuid.uuid4())


def register_device(device_id=None, name=''):
    """配对成功后注册设备并返回 {deviceId, token, name, pairedAt}。"""
    user = get_single_user()
    device_id = device_id or generate_device_id()
    token = generate_token()
    created = now_iso()
    db = get_conn(CORE_DB)
    db.execute(
        '''INSERT INTO devices (device_id, token_hash, user_id, name, created_at, last_active)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (device_id, hash_token(token), user['user_id'], name, created, created)
    )
    db.commit()
    return {
        'token': token,
        'deviceId': device_id,
        'name': name,
        'pairedAt': created,
    }
