"""签名临时 URL（ARCHITECTURE §6.8）：HMAC-SHA256 短时有效，供外部播放器/地址栏免鉴权下载。

- sig = HMAC-SHA256(密钥, `share|path|exp`)
- 密钥：环境变量 SIGN_SECRET 优先；否则首次使用自动生成并持久化到 core.db app_config
  （泄露需轮换：改环境变量，或重置 app_config 的 drive_sign_secret）。
"""
import hashlib
import hmac
import secrets
import time

from config import CORE_DB, SIGN_SECRET, SIGN_URL_TTL
from core.db import get_conn

_KEY_NAME = 'drive_sign_secret'


def _get_secret():
    if SIGN_SECRET:
        return SIGN_SECRET.encode('utf-8')
    db = get_conn(CORE_DB)
    row = db.execute('SELECT value FROM app_config WHERE key = ?', (_KEY_NAME,)).fetchone()
    if row:
        return row['value'].encode('utf-8')
    secret = secrets.token_hex(32)
    db.execute('INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)', (_KEY_NAME, secret))
    db.commit()
    return secret.encode('utf-8')


def sign(share, path, ttl=None):
    """生成签名：返回 (exp, sig)。"""
    ttl = ttl if ttl is not None else SIGN_URL_TTL
    exp = int(time.time()) + ttl
    msg = f'{share}|{path}|{exp}'.encode('utf-8')
    sig = hmac.new(_get_secret(), msg, hashlib.sha256).hexdigest()
    return exp, sig


def verify(share, path, exp, sig):
    """校验签名：exp 未过期 + sig 匹配。任一缺失/无效返回 False（fail-closed）。"""
    if not sig:
        return False
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    if int(time.time()) > exp_i:
        return False
    msg = f'{share}|{path}|{exp_i}'.encode('utf-8')
    expect = hmac.new(_get_secret(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, str(sig))
