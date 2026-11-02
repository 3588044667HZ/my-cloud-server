"""统一响应协议：{code, msg, data} 与分页、错误码表。

全部 /api 响应走 success()/error()；错误码统一前缀：
- 0 段：通用业务码
- 104xx：attachment 域（沿用 mock-server ATTACHMENT_TWO_PHASE_UPLOAD_SPEC）
- 10401 同时用于设备 token 未认证（UNIFIED_DEVICE_TOKEN_AUTH_SPEC §2.3）
"""
from flask import jsonify


# ---------- 通用 ----------
CODE_OK = 0

# ---------- 鉴权 ----------
CODE_UNAUTHORIZED = 10401          # token 缺失/无效/已撤销
CODE_FORBIDDEN = 10403             # 设备有效但无权限（如云盘未开启）
CODE_NOT_FOUND = 10404

# ---------- attachment 域 ----------
CODE_ATTACH_CONFLICT = 10402       # attachId 已占用且内容不匹配
CODE_ATTACH_NOT_FOUND = 10404      # 补传时记录不存在
CODE_ATTACH_MD5_MISMATCH = 10405   # Content-MD5 不匹配
CODE_ATTACH_TOO_LARGE = 10413      # 超过大小上限
CODE_ATTACH_BAD_TYPE = 10415       # type 不支持

# ---------- 分片上传域（SPEC-UPLOAD-TRANSPORT.md） ----------
CODE_CHUNK_CRC = 10418             # 单块 CRC32 不符 → 重传该块
CODE_DISK_FULL = 10507             # 磁盘满保护


def success(data=None, extra=None):
    """成功响应：{code:0, msg:'success', data}，可带额外字段（如 pagination）。"""
    body = {'code': CODE_OK, 'msg': 'success', 'data': data}
    if extra:
        body.update(extra)
    return jsonify(body)


def error(msg, code=1, status=400):
    """错误响应。status 为 HTTP 状态码；code 为业务错误码（缺省与业务语义相关）。"""
    return jsonify({'code': code, 'msg': msg, 'data': None}), status


def pagination(page, size, total):
    """分页字段（沿用 notes 契约）。"""
    return {
        'pagination': {
            'page': page,
            'size': size,
            'total': total,
            'totalPages': max(1, (total + size - 1) // size),
        }
    }
