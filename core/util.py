"""通用工具：时间戳 / id 生成 / CRC32。"""
import time as _time
import uuid
import zlib
from datetime import datetime


def now_iso():
    """UTC 时间 ISO8601（沿用 mock-server 格式 %Y-%m-%dT%H:%M:%SZ）。"""
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')


def utc_ts():
    return int(_time.time())


def generate_id(prefix='n'):
    """服务端生成短 id（沿用 mock-server 格式 n{uuid.hex[:10]}）。"""
    return f'{prefix}{uuid.uuid4().hex[:10]}'


def crc32_hex(data):
    """单块 CRC32（大文件分片传输，SPEC-UPLOAD-TRANSPORT.md §2 块级校验）。"""
    return format(zlib.crc32(data) & 0xffffffff, '08x')
